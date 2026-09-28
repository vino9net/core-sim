"""Environment-driven settings.

Everything is env-configurable because the engine and store are meant to be swapped
between benchmark runs without a rebuild (ARCH_DESIGN.md D3, D7).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# NOTE: no slots=True. With slots, `cls.<field>` returns the slot *descriptor* rather
# than the default value, so every `from_env()` fallback below would silently be a
# descriptor instead of the intended default. Settings is built once at startup; slots
# would buy nothing anyway.
@dataclass(frozen=True)
class Settings:
    # --- engine selection (ARCH_DESIGN.md D7) ---
    engine: str = "redis_lua"

    # --- store ---
    # Dragonfly is a drop-in swap: point this at its port. See ARCH_DESIGN.md §8.1 —
    # the Streams/consumer-group spike has NOT been done yet.
    redis_url: str = "redis://localhost:6379"
    # Redis runs commands on one thread, so a deeper pool buys no throughput — it only
    # buys queueing. Size to keep the socket busy, not to match request concurrency.
    redis_max_connections: int = 64
    # Seconds to wait for a free connection before erroring. Excess concurrency queues
    # (backpressure) rather than failing fast with a misleading 500.
    redis_pool_timeout: float = 5.0

    # --- outbox stream (ARCH_DESIGN.md D4) ---
    stream_key: str = "transfers"
    # THE knob that can still lose data. Stream is the relay's buffer, so this is
    # relay-downtime tolerance. ~1M entries ≈ 10s @ 100k tps ≈ 100MB RAM.
    # Trim below relay lag and transfers vanish with no error anywhere.
    stream_maxlen: int = 1_000_000
    idem_ttl_seconds: int = 86_400

    # --- relay (ARCH_DESIGN.md D5) ---
    # Empty = publishing disabled: the relay still drains the outbox stream and XACKs
    # it, just without shipping anywhere. Lets you run the Redis-side engine benchmark
    # with no AWS account in the loop at all. Set to a stream name to enable.
    #
    # Region and endpoint are deliberately not settings here — boto3 already resolves
    # both itself (AWS_REGION/AWS_DEFAULT_REGION/~/.aws/config/instance metadata for
    # region; AWS_ENDPOINT_URL_KINESIS for a LocalStack override), the same way it
    # already resolves credentials without us reading AWS_ACCESS_KEY_ID ourselves.
    kinesis_stream: str = ""
    relay_group: str = "relay"
    relay_consumer: str = "relay-1"
    relay_batch: int = 1_000
    relay_block_ms: int = 100

    # --- server ---
    host: str = "0.0.0.0"  # noqa: S104 - must be reachable from other VMs in the VPC/SG
    port: int = 8000

    # --- logging ---
    log_level: str = "INFO"
    log_json: bool = True
    # OFF by default and that is deliberate: per-request logging costs ~10-50µs, which
    # at 100k tps dominates the very thing we are trying to measure. Turn on to debug,
    # never during a run.
    log_requests: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            engine=_env("ENGINE", cls.engine),
            redis_url=_env("REDIS_URL", cls.redis_url),
            redis_max_connections=_env_int(
                "REDIS_MAX_CONNECTIONS", cls.redis_max_connections
            ),
            redis_pool_timeout=float(
                os.getenv("REDIS_POOL_TIMEOUT", str(cls.redis_pool_timeout))
            ),
            stream_key=_env("STREAM_KEY", cls.stream_key),
            stream_maxlen=_env_int("STREAM_MAXLEN", cls.stream_maxlen),
            idem_ttl_seconds=_env_int("IDEM_TTL_SECONDS", cls.idem_ttl_seconds),
            kinesis_stream=_env("KINESIS_STREAM", cls.kinesis_stream),
            relay_group=_env("RELAY_GROUP", cls.relay_group),
            relay_consumer=_env(
                "RELAY_CONSUMER", os.getenv("HOSTNAME", cls.relay_consumer)
            ),
            relay_batch=_env_int("RELAY_BATCH", cls.relay_batch),
            relay_block_ms=_env_int("RELAY_BLOCK_MS", cls.relay_block_ms),
            host=_env("HOST", cls.host),
            port=_env_int("PORT", cls.port),
            log_level=_env("LOG_LEVEL", cls.log_level).upper(),
            log_json=_env_bool("LOG_JSON", cls.log_json),
            log_requests=_env_bool("LOG_REQUESTS", cls.log_requests),
        )
