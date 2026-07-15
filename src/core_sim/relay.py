"""Relay: Redis Stream (outbox) -> NATS JetStream (log of record).

ARCH_DESIGN.md D5. Deliberately small and in-house rather than Redpanda Connect,
because the relay is a *measured component*: we want our own lag/batch/ack numbers and
a loop we can read when a run looks weird.

Two invariants, both easy to break and neither loud when broken:

* **Publish, then XACK.** Never the reverse. Crash in between and entries stay in the
  Pending Entries List, so a restarted relay re-sends them. At-least-once; consumers
  dedupe on transfer id. XACK-first would silently drop on crash.
* **Drain the PEL before taking new entries.** Start at cursor "0" until it comes back
  empty, then switch to ">". Starting at ">" skips your own pending entries and they
  sit in the PEL forever — silently, and only after a restart.

Batching is free here: XREADGROUP(count=N, block=ms) returns full batches under load
(ack cost amortises to ~nothing) and small ones when idle (low latency). No timer.

Deployment note: this is a background loop, NOT request-driven. It must run as a plain
K8s Deployment. A Knative Service would scale it to zero when no HTTP traffic arrives
and relaying would silently stop (ARCH_DESIGN.md §7.2).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import cast

import nats
from nats.js.errors import BadRequestError
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from core_sim.config import Settings
from core_sim.logging import configure_logging, get_logger
from core_sim.record import pack, str_to_ulid

log = get_logger("core_sim.relay")


class Relay:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._stop = asyncio.Event()

    async def run(self) -> None:
        s = self._s
        r = Redis.from_url(s.redis_url, decode_responses=False)
        nc = await nats.connect(s.nats_url)
        js = nc.jetstream()

        try:
            await js.add_stream(name=s.nats_stream, subjects=[s.nats_subject])
            log.info("relay.stream_created", stream=s.nats_stream)
        except BadRequestError:
            pass  # already exists

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
            subject=s.nats_subject,
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
                msgs = cast("list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]", raw)
                entries = msgs[0][1] if msgs else []

                if not entries:
                    if cursor == "0":
                        log.info("relay.pel_drained", resumed=published)
                        cursor = ">"
                    continue

                payload = b"".join(_to_record(fields) for _, fields in entries)
                await js.publish(s.nats_subject, payload)  # ack lands before XACK
                await r.xack(s.stream_key, s.relay_group, *[eid for eid, _ in entries])

                published += len(entries)
                log.debug("relay.batch", n=len(entries), total=published)
        finally:
            await r.aclose()
            await nc.drain()
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
    )


async def _amain() -> None:
    settings = Settings.from_env()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    relay = Relay(settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, relay.stop)

    await relay.run()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain())


if __name__ == "__main__":
    main()
