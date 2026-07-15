"""Engine interface + registry.

The whole rig exists to compare implementations behind this one interface
(ARCH_DESIGN.md D7). The comparison *is* the product — a single number with nothing to
contrast it against is not useful output.

Planned engines and their expected shape on an 8-core VM:

    engine              uniform     hot account
    pg_naive            ~5k         ~300        measured 3-5k in prototype (the only
                                                calibrated point in the matrix)
    pg_sproc            ~30k        ~1.5k
    pg_sharded          ~35k        ~20k
    pg_batched          ~50k        ~50k
    redis_lua           ~80-120k    ~80-120k    <- implemented
    redis_batched       ~300-500k   ~300-500k
    dragonfly_batched   measure     measure     config swap of redis_*

Keep the pg_* engines. Redis makes hot-account contention structurally impossible
(single writer), so it *deletes* the contention experiment — pg_* is where that story
lives.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core_sim.config import Settings
from core_sim.models import Account, ConservationReport, TransferRequest, TransferResult


class Engine(ABC):
    """One storage strategy for the transfer workload."""

    name: str = "abstract"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def get_account(self, account_id: int) -> Account | None: ...

    @abstractmethod
    async def transfer(self, req: TransferRequest) -> TransferResult: ...

    @abstractmethod
    async def seed(self, n_accounts: int, opening_balance: int, currency: str) -> None:
        """Reseed from scratch. First-class rather than a script in someone's shell
        history — balances are derived state and this gets run constantly
        (ARCH_DESIGN.md §6.3)."""

    @abstractmethod
    async def conservation(self) -> ConservationReport:
        """Sum every balance. Transfers move money; they never create or destroy it.

        If this drifts, the engine is broken and its throughput number is fiction
        (ARCH_DESIGN.md §6.1). O(N) — an admin endpoint, never the hot path.
        """


def build_engine(settings: Settings) -> Engine:
    # Imported lazily so the Postgres extra stays optional.
    if settings.engine in {"redis_lua", "dragonfly_lua"}:
        from core_sim.engines.redis_lua import RedisLuaEngine

        return RedisLuaEngine(settings)

    known = {"redis_lua", "dragonfly_lua"}
    planned = {"redis_batched", "pg_naive", "pg_sproc", "pg_sharded", "pg_batched"}
    if settings.engine in planned:
        raise NotImplementedError(
            f"engine {settings.engine!r} is in the design matrix but not built yet; "
            f"available now: {sorted(known)}"
        )
    raise ValueError(f"unknown engine {settings.engine!r}; available: {sorted(known)}")


__all__ = ["Engine", "build_engine"]
