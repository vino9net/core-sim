"""redis_lua — one EVALSHA per transfer (ARCH_DESIGN.md D3, D4).

Three things here are load-bearing, and getting any of them wrong is how the earlier
prototype ended up *slower than Postgres* (see ARCH_DESIGN.md §6.1):

1. ``redis.asyncio``, never the sync client. A sync client inside an async handler
   blocks the event loop and serialises the whole process — no request overlap at all.
   This is the single most likely cause of that result.
2. ``redis[hiredis]`` — the C protocol parser. The pure-Python RESP parser is typically
   2-3x more client-side overhead, and asyncpg (Cython, binary protocol) is a high bar
   to clear.
3. The transfer is *one* round trip and atomic by construction. Never read-modify-write:
   GET/check/SET across concurrent requests is a lost-update race that silently creates
   money, and benchmarking a broken implementation measures nothing.
"""

from __future__ import annotations

import random
from pathlib import Path

from redis.asyncio import BlockingConnectionPool, Redis
from redis.commands.core import AsyncScript

from core_sim.config import Settings
from core_sim.engines import Engine
from core_sim.logging import get_logger
from core_sim.models import (
    Account,
    ConservationReport,
    TransferRequest,
    TransferResult,
    TransferStatus,
)
from core_sim.record import new_ulid, ulid_to_str

log = get_logger(__name__)

_LUA_PATH = Path(__file__).parent / "lua" / "transfer.lua"
_SEED_CHUNK = 5_000

# Customer-to-account ratio: most customers hold one account, a shrinking tail holds
# up to 5. Weights sum to 1.0.
_ACCOUNTS_PER_CUSTOMER = [1, 2, 3, 4, 5]
_ACCOUNTS_PER_CUSTOMER_WEIGHTS = [0.70, 0.18, 0.07, 0.03, 0.02]
# Fixed, not random.Random() default-seeded: balances are derived state that a reseed
# must reproduce exactly (ARCH_DESIGN.md §6.3), and account-to-customer ownership is
# now part of that derived state too.
_CUSTOMER_ASSIGNMENT_SEED = 1337


def _assign_customer_ids(n_accounts: int) -> list[int]:
    """One customer id (1-based) per account index 0..n_accounts-1."""
    rng = random.Random(_CUSTOMER_ASSIGNMENT_SEED)
    customer_ids: list[int] = []
    next_customer_id = 1
    while len(customer_ids) < n_accounts:
        size = rng.choices(_ACCOUNTS_PER_CUSTOMER, weights=_ACCOUNTS_PER_CUSTOMER_WEIGHTS)[0]
        size = min(size, n_accounts - len(customer_ids))
        customer_ids.extend([next_customer_id] * size)
        next_customer_id += 1
    return customer_ids


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _text(v: bytes | str) -> str:
    """redis-py's types say ``bytes | str`` because decode_responses is a runtime flag
    its stubs can't see. We set it False, so replies are bytes — but assert that through
    a helper rather than a blanket ignore, so a future decode_responses=True does not
    silently produce ``b'SGD'``-shaped strings."""
    return v.decode() if isinstance(v, bytes) else v


class RedisLuaEngine(Engine):
    name = "redis_lua"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._redis: Redis | None = None
        self._pool: BlockingConnectionPool | None = None
        self._script: AsyncScript | None = None

    # Accessors rather than bare attribute reads: the client only exists after start(),
    # and these turn "used before start()" into a clear error at the call site instead
    # of an AttributeError on None halfway through a request.

    @property
    def _r(self) -> Redis:
        if self._redis is None:
            raise RuntimeError(f"{self.name}: engine not started — call start() first")
        return self._redis

    @property
    def _xfer(self) -> AsyncScript:
        if self._script is None:
            raise RuntimeError(f"{self.name}: engine not started — call start() first")
        return self._script

    async def start(self) -> None:
        # BlockingConnectionPool, NOT the default ConnectionPool. The default *raises*
        # MaxConnectionsError the moment concurrent requests exceed max_connections,
        # which under load surfaces as spurious 500s that look like Redis failing. We
        # want excess concurrency to queue — that is backpressure, and the wait shows up
        # honestly in p99 instead of as a fake error.
        #
        # Sizing note: Redis executes commands on ONE thread, so a bigger pool does not
        # buy throughput — commands serialise regardless. The pool only needs to be deep
        # enough to keep the socket busy; past that it is pure queueing. This matters
        # under Knative, where high containerConcurrency (which we want, for batching)
        # means large request bursts per pod (ARCH_DESIGN.md §7.2).
        self._pool = BlockingConnectionPool.from_url(
            self._s.redis_url,
            max_connections=self._s.redis_max_connections,
            timeout=self._s.redis_pool_timeout,
            # bytes, not str: decoding every reply costs more than the two int() calls
            # we do ourselves on the hot path.
            decode_responses=False,
        )
        self._redis = Redis(connection_pool=self._pool)
        await self._redis.ping()
        # register_script -> EVALSHA, with automatic EVAL fallback on NOSCRIPT (which
        # happens after a Redis restart or SCRIPT FLUSH).
        self._script = self._redis.register_script(_LUA_PATH.read_text())
        info = await self._redis.info("server")
        log.info(
            "engine.started",
            engine=self.name,
            server=info.get("redis_version") or info.get("dragonfly_version"),
            url=self._s.redis_url,
        )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
        if self._pool is not None:
            await self._pool.aclose()
        log.info("engine.stopped", engine=self.name)

    # --- reads -------------------------------------------------------------

    async def get_account(self, account_id: int) -> Account | None:
        raw = await self._r.hgetall(f"acct:{account_id}")
        if not raw:
            return None
        return Account(
            account_id=account_id,
            account_no=_text(raw[b"account_no"]),
            currency=_text(raw[b"currency"]),
            balance=int(raw[b"balance"]),
            avail_balance=int(raw[b"avail_balance"]),
            customer_id=int(raw[b"customer_id"]),
            status=int(raw[b"status"]),
        )

    # --- the hot path ------------------------------------------------------

    async def transfer(self, req: TransferRequest) -> TransferResult:
        xfer_id = ulid_to_str(new_ulid())
        created = _now_ms()

        status, returned_id, ts, from_cid, to_cid = await self._xfer(
            keys=[
                f"acct:{req.from_account}",
                f"acct:{req.to_account}",
                f"idem:{req.idempotency_key}" if req.idempotency_key else "",
                self._s.stream_key,
            ],
            args=[
                req.amount,
                xfer_id,
                req.currency,
                req.memo,
                self._s.stream_maxlen,
                created,
                req.from_account,
                req.to_account,
                self._s.idem_ttl_seconds,
            ],
        )

        transfer_id = _text(returned_id) if returned_id else None
        return TransferResult(
            status=TransferStatus(int(status)),
            transfer_id=transfer_id or None,
            created_at=int(ts) if ts else created,
            from_customer_id=int(from_cid) if from_cid else 0,
            to_customer_id=int(to_cid) if to_cid else 0,
        )

    # --- admin -------------------------------------------------------------

    async def _delete_scoped(self, patterns: list[str]) -> int:
        """UNLINK every key matching these patterns. Bounded to our own keyspace."""
        deleted = 0
        for pattern in patterns:
            if "*" not in pattern:  # literal key, no scan needed
                deleted += await self._r.unlink(pattern)
                continue
            batch: list[bytes] = []
            async for key in self._r.scan_iter(match=pattern, count=1000):
                batch.append(key)
                if len(batch) >= 1000:
                    deleted += await self._r.unlink(*batch)
                    batch = []
            if batch:
                deleted += await self._r.unlink(*batch)
        return deleted

    async def seed(self, n_accounts: int, opening_balance: int, currency: str) -> None:
        # Scoped delete, NOT FLUSHDB. Seeding is destructive by nature, but it has no
        # business destroying keys it does not own — REDIS_URL may well point at a Redis
        # shared with something else (a local Homebrew instance, say), and db 0 is the
        # default. Only our own keyspace goes.
        #
        # The outbox stream is included deliberately: a reseed is a fresh start, and
        # stale stream entries would poison the next conservation check. Dropping the
        # stream drops the relay's consumer group with it; the relay recreates it via
        # mkstream=True on its next read.
        #
        # UNLINK rather than DEL — it reclaims memory on a background thread, so a large
        # reseed does not stall Redis's single command thread.
        await self._delete_scoped(["acct:*", "idem:*", self._s.stream_key])
        customer_ids = _assign_customer_ids(n_accounts)
        for base in range(0, n_accounts, _SEED_CHUNK):
            pipe = self._r.pipeline(transaction=False)
            for i in range(base, min(base + _SEED_CHUNK, n_accounts)):
                pipe.hset(
                    f"acct:{i}",
                    mapping={
                        "account_no": f"{i:012d}",
                        "currency": currency,
                        "balance": opening_balance,
                        "avail_balance": opening_balance,
                        "customer_id": customer_ids[i],
                        "status": 1,
                    },
                )
            await pipe.execute()
        log.info(
            "engine.seeded",
            engine=self.name,
            n_accounts=n_accounts,
            n_customers=customer_ids[-1] if customer_ids else 0,
            opening_balance=opening_balance,
            currency=currency,
            expected_total=n_accounts * opening_balance,
        )

    async def conservation(self) -> ConservationReport:
        total = 0
        n = 0
        batch: list[bytes] = []

        async def drain(keys: list[bytes]) -> tuple[int, int]:
            pipe = self._r.pipeline(transaction=False)
            for k in keys:
                pipe.hget(k, "balance")
            vals = await pipe.execute()
            return sum(int(v) for v in vals if v is not None), len(keys)

        async for key in self._r.scan_iter(match="acct:*", count=1000):
            batch.append(key)
            if len(batch) >= 1000:
                t, c = await drain(batch)
                total += t
                n += c
                batch = []
        if batch:
            t, c = await drain(batch)
            total += t
            n += c

        return ConservationReport(n_accounts=n, total_balance=total)
