"""End-to-end against a real Redis. Skipped if one isn't reachable.

The conservation test here is the important one — it is the property that makes every
throughput number the rig produces trustworthy (ARCH_DESIGN.md §6.1).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from core_sim.config import Settings
from core_sim.engines.redis_lua import RedisLuaEngine
from core_sim.models import TransferRequest, TransferStatus

REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379")
OPENING = 1_000_000
N = 200


def _redis_available() -> bool:
    async def ping() -> bool:
        from redis.asyncio import Redis

        r = Redis.from_url(REDIS_URL)
        try:
            await asyncio.wait_for(r.ping(), timeout=1.0)
            return True
        except Exception:
            return False
        finally:
            await r.aclose()

    try:
        return asyncio.run(ping())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(),
    reason=f"no redis at {REDIS_URL} (try: brew services start redis)",
)


@pytest.fixture
async def engine():
    # db 15 so a stray run never clobbers a dev dataset — seed() calls FLUSHDB.
    settings = Settings(redis_url=f"{REDIS_URL}/15", stream_key="test:transfers")
    eng = RedisLuaEngine(settings)
    await eng.start()
    await eng.seed(N, OPENING, "SGD")
    yield eng
    await eng.close()


def _req(frm: int, to: int, amount: int, **kw) -> TransferRequest:
    kw.setdefault("idempotency_key", str(uuid.uuid4()))
    return TransferRequest(
        from_account=frm, to_account=to, amount=amount, currency="SGD", memo="test", **kw
    )


async def test_happy_path(engine):
    res = await engine.transfer(_req(0, 1, 500))
    assert res.status is TransferStatus.OK
    assert res.transfer_id and len(res.transfer_id) == 26

    a, b = await engine.get_account(0), await engine.get_account(1)
    assert a.balance == OPENING - 500
    assert a.avail_balance == OPENING - 500
    assert b.balance == OPENING + 500
    assert res.from_customer_id == a.customer_id
    assert res.to_customer_id == b.customer_id
    assert a.customer_id > 0
    assert b.customer_id > 0


async def test_insufficient_funds_is_rejected_not_overdrawn(engine):
    res = await engine.transfer(_req(0, 1, OPENING + 1))
    assert res.status is TransferStatus.INSUFFICIENT_FUNDS
    assert (await engine.get_account(0)).balance == OPENING


async def test_unknown_account(engine):
    assert (
        await engine.transfer(_req(0, 999_999, 10))
    ).status is TransferStatus.ACCOUNT_NOT_FOUND


async def test_currency_mismatch(engine):
    req = TransferRequest(
        from_account=0,
        to_account=1,
        amount=10,
        currency="USD",
        idempotency_key=str(uuid.uuid4()),
    )
    assert (await engine.transfer(req)).status is TransferStatus.CURRENCY_MISMATCH


async def test_self_transfer_rejected(engine):
    result = await engine.transfer(_req(0, 0, 10))
    assert result.status is TransferStatus.INSUFFICIENT_FUNDS


async def test_idempotent_replay_returns_original(engine):
    key = str(uuid.uuid4())
    first = await engine.transfer(_req(0, 1, 500, idempotency_key=key))
    second = await engine.transfer(_req(0, 1, 500, idempotency_key=key))

    assert first.status is TransferStatus.OK
    assert second.status is TransferStatus.DUPLICATE
    assert second.transfer_id == first.transfer_id  # the *original*, not a new one
    # The money moved exactly once. This is the whole point of the idempotency key.
    assert (await engine.get_account(0)).balance == OPENING - 500


async def test_failed_transfer_does_not_burn_idempotency_key(engine):
    """A retry after a transient failure must be able to succeed."""
    key = str(uuid.uuid4())
    assert (
        await engine.transfer(_req(0, 1, OPENING + 1, idempotency_key=key))
    ).status is TransferStatus.INSUFFICIENT_FUNDS
    assert (
        await engine.transfer(_req(0, 1, 10, idempotency_key=key))
    ).status is TransferStatus.OK


async def test_seed_assigns_customer_ids_with_long_tail(engine):
    """Most customers own exactly 1 account; a shrinking tail owns up to 5, never more."""
    accounts = await asyncio.gather(*(engine.get_account(i) for i in range(N)))
    by_customer: dict[int, int] = {}
    for a in accounts:
        assert a.customer_id > 0
        by_customer[a.customer_id] = by_customer.get(a.customer_id, 0) + 1

    counts = list(by_customer.values())
    assert sum(counts) == N
    assert max(counts) <= 5
    # Single-account customers should dominate (weighted ~70% by construction).
    assert sum(1 for c in counts if c == 1) > sum(1 for c in counts if c > 1)


async def test_seed_does_not_touch_foreign_keys(engine):
    """seed() is destructive by nature but must stay inside its own keyspace.

    REDIS_URL may point at a Redis shared with something else — a local Homebrew
    instance on db 0, say. A FLUSHDB here would take that with it.
    """
    await engine._r.set("someone-elses-key", "precious")
    try:
        await engine.seed(N, OPENING, "SGD")
        assert await engine._r.get("someone-elses-key") == b"precious"
        # ...while our own keyspace really was reset.
        assert (await engine.conservation()).total_balance == N * OPENING
    finally:
        await engine._r.unlink("someone-elses-key")


async def test_outbox_entry_written_atomically(engine):
    """The XADD is the reason the sim pod can hold no state (ARCH_DESIGN.md D4)."""
    before = await engine._redis.xlen("test:transfers")
    await engine.transfer(_req(0, 1, 500))
    assert await engine._redis.xlen("test:transfers") == before + 1


async def test_conservation_under_concurrency(engine):
    """ARCH_DESIGN.md §6.1 — money is moved, never created or destroyed.

    This is the test that would have caught a read-modify-write implementation: under
    concurrency a GET/check/SET loses updates and silently mints money.
    """
    expected = N * OPENING
    assert (await engine.conservation()).total_balance == expected

    # Hammer a hot pair from many coroutines at once.
    await asyncio.gather(
        *(engine.transfer(_req(i % 10, 10 + (i % 10), 13)) for i in range(500))
    )

    report = await engine.conservation()
    assert report.n_accounts == N
    assert report.total_balance == expected, (
        f"conservation violated: drift={report.total_balance - expected}"
    )
