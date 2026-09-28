"""Relay: Redis Stream (outbox) -> Amazon Kinesis Data Streams (log of record).

ARCH_DESIGN.md D5. Deliberately small and in-house rather than a managed connector,
because the relay is a *measured component*: we want our own lag/batch/ack numbers and
a loop we can read when a run looks weird.

This repo only publishes. Whatever consumes the Kinesis stream — fanning a transfer
out into a debit-account / credit-account DynamoDB item pair, or anything else — lives
in a separate repo/consumer, not here.

Two invariants, both easy to break and neither loud when broken:

* **Publish, then XACK.** Never the reverse. Crash in between and entries stay in the
  Pending Entries List, so a restarted relay re-sends them. At-least-once; consumers
  dedupe on transfer id. XACK-first would silently drop on crash.
* **Drain the PEL before taking new entries.** Start at cursor "0" until it comes back
  empty, then switch to ">". Starting at ">" skips your own pending entries and they
  sit in the PEL forever — silently, and only after a restart.

Batching is free here: XREADGROUP(count=N, block=ms) returns full batches under load
(ack cost amortises to ~nothing) and small ones when idle (low latency). No timer.

Publishing is optional and named by a single knob: the ``KINESIS_STREAM`` env var (or
``--publish STREAM``/``--no-publish`` on the CLI, which take priority over the env var).
Empty/unset means disabled — the relay still drains the outbox stream and XACKs it, it
just skips the network hop. Region and endpoint are not settings here; boto3 already
resolves both itself (same chain it already uses for credentials).

Deployment note: this is a background loop, NOT request-driven. It must run as a plain
K8s Deployment. A Knative Service would scale it to zero when no HTTP traffic arrives
and relaying would silently stop (ARCH_DESIGN.md §7.2).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import signal
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from core_sim.config import Settings
from core_sim.logging import configure_logging, get_logger
from core_sim.record import pack, str_to_ulid

log = get_logger("core_sim.relay")

# Hard AWS limit: at most 500 records (and 5MB total) per PutRecords call.
_KINESIS_PUT_RECORDS_MAX = 500


class Relay:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._stop = asyncio.Event()

    async def run(self) -> None:
        s = self._s
        r = Redis.from_url(s.redis_url, decode_responses=False)

        kinesis: Any = None
        if s.kinesis_stream:
            import boto3  # lazy: only relays that actually publish need the AWS SDK

            kinesis = boto3.client("kinesis")  # region/endpoint resolved by boto3 itself
        else:
            log.warning(
                "relay.publish_disabled", note="draining and acking, not publishing"
            )

        try:
            await r.xgroup_create(s.stream_key, s.relay_group, id="0", mkstream=True)
            log.info("relay.group_created", stream=s.stream_key, group=s.relay_group)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

        log.info(
            "relay.started",
            stream=s.stream_key,
            group=s.relay_group,
            consumer=s.relay_consumer,
            kinesis_stream=s.kinesis_stream if kinesis is not None else None,
            batch=s.relay_batch,
        )

        # "0" = our own pending entries first. Only switch to ">" once drained.
        cursor = "0"
        published = 0
        try:
            while not self._stop.is_set():
                raw = await r.xreadgroup(
                    groupname=s.relay_group,
                    consumername=s.relay_consumer,
                    streams={s.stream_key: cursor},
                    count=s.relay_batch,
                    block=s.relay_block_ms,
                )
                # redis-py types xreadgroup as a broad union that does not match what it
                # actually returns: a list of (stream_name, [(entry_id, fields), ...]).
                # Narrow it once, here, rather than fighting the stub at every use.
                msgs = cast(
                    "list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]", raw
                )
                entries = msgs[0][1] if msgs else []

                if not entries:
                    if cursor == "0":
                        log.info("relay.pel_drained", resumed=published)
                        cursor = ">"
                    continue

                if kinesis is not None:
                    # boto3 is sync; keep it off the event loop.
                    acked_ids = await asyncio.to_thread(
                        _publish_batch, kinesis, s.kinesis_stream, entries
                    )
                else:
                    acked_ids = [eid for eid, _ in entries]

                if acked_ids:
                    await r.xack(s.stream_key, s.relay_group, *acked_ids)

                published += len(acked_ids)
                log.info("relay.batch", n=len(acked_ids), total=published)
        finally:
            await r.aclose()
            log.info("relay.stopped", published=published)

    def stop(self) -> None:
        self._stop.set()


def _to_record(fields: dict[bytes, bytes]) -> bytes:
    return pack(
        ulid=str_to_ulid(fields[b"id"].decode()),
        from_account=int(fields[b"f"]),
        to_account=int(fields[b"t"]),
        amount=int(fields[b"a"]),
        currency=fields[b"c"].decode(),
        created_at=int(fields[b"ts"]),
        status=1,
        memo=fields.get(b"m", b"").decode("utf-8", errors="replace"),
        from_customer_id=int(fields.get(b"fc") or 0),
        to_customer_id=int(fields.get(b"tc") or 0),
    )


def _publish_batch(
    kinesis: Any, stream_name: str, entries: list[tuple[bytes, dict[bytes, bytes]]]
) -> list[bytes]:
    """One Kinesis record per transfer (not concatenated like the old NATS batch) —
    an event-source-mapping consumer gets one event per Kinesis record, and a 1:1
    mapping to transfers is what a downstream fan-out consumer wants to iterate over.

    Partition key is the transfer id, not an account id: partitioning by account would
    put a hot account's transfers all on one shard, which is exactly the contention
    this rig is built to stress-test (ARCH_DESIGN.md D8's Zipf knob).

    Returns the entry ids that were confirmed published. A record that fails stays
    un-acked and sits in the PEL for redelivery on the next relay restart — the same
    at-least-once behaviour a mid-batch crash would produce (see module docstring).
    """
    acked: list[bytes] = []
    for i in range(0, len(entries), _KINESIS_PUT_RECORDS_MAX):
        chunk = entries[i : i + _KINESIS_PUT_RECORDS_MAX]
        records = [
            {"Data": _to_record(fields), "PartitionKey": fields[b"id"].decode()}
            for _, fields in chunk
        ]
        resp = kinesis.put_records(StreamName=stream_name, Records=records)
        for (eid, _), result in zip(chunk, resp["Records"], strict=True):
            if "ErrorCode" in result:
                log.error(
                    "relay.publish_failed",
                    entry_id=eid,
                    error_code=result["ErrorCode"],
                    error_message=result.get("ErrorMessage"),
                )
            else:
                acked.append(eid)
    return acked


async def _amain(stream_override: str | None) -> None:
    settings = Settings.from_env()
    if stream_override is not None:
        settings = dataclasses.replace(settings, kinesis_stream=stream_override)
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    relay = Relay(settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, relay.stop)

    await relay.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Redis Streams -> Kinesis relay")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--publish",
        metavar="STREAM",
        help="Kinesis stream to publish to. Overrides KINESIS_STREAM.",
    )
    group.add_argument(
        "--no-publish",
        action="store_true",
        help="Disable publishing even if KINESIS_STREAM is set.",
    )
    args = parser.parse_args()

    stream_override = "" if args.no_publish else args.publish  # None = defer to env var

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(stream_override))


if __name__ == "__main__":
    main()
