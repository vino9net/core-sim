"""REST surface.

    GET  /accounts/{account_id}   account detail incl. balance
    POST /transfer                create a transfer, returns detail incl. id

Plus ops endpoints that the rig needs: /health, /admin/seed, /admin/conservation.
"""

from __future__ import annotations

from litestar import Response, Router, get, post
from litestar.datastructures import State
from litestar.exceptions import HTTPException, NotFoundException

from core_sim.engines import Engine
from core_sim.logging import get_logger
from core_sim.models import (
    STATUS_HTTP,
    Account,
    ConservationReport,
    SeedRequest,
    Transfer,
    TransferRequest,
    TransferStatus,
)

log = get_logger(__name__)


async def provide_engine(state: State) -> Engine:
    return state.engine


@get("/health", sync_to_thread=False, summary="Liveness")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@get("/accounts/{account_id:int}", summary="Account detail")
async def get_account(account_id: int, engine: Engine) -> Account:
    account = await engine.get_account(account_id)
    if account is None:
        raise NotFoundException(detail=f"account {account_id} not found")
    return account


@post("/transfer", summary="Create a fund transfer")
async def create_transfer(data: TransferRequest, engine: Engine) -> Response[Transfer]:
    result = await engine.transfer(data)
    http_status = STATUS_HTTP[result.status]

    if result.status in {TransferStatus.OK, TransferStatus.DUPLICATE}:
        return Response(
            Transfer(
                id=result.transfer_id or "",
                from_account=data.from_account,
                to_account=data.to_account,
                amount=data.amount,
                currency=data.currency,
                memo=data.memo,
                status=result.status.name,
                created_at=result.created_at,
                from_customer_id=result.from_customer_id,
                to_customer_id=result.to_customer_id,
            ),
            status_code=http_status,
        )

    # Failures carry the machine-readable reason; the client (and k6) checks on it.
    raise HTTPException(
        status_code=http_status,
        detail=result.status.name,
        extra={"reason": result.status.name},
    )


@post("/admin/seed", summary="Reseed accounts from scratch")
async def seed(data: SeedRequest, engine: Engine) -> ConservationReport:
    """Destructive. First-class on purpose — balances are derived state, so this is the
    recovery path, not a convenience (ARCH_DESIGN.md §6.3)."""
    await engine.seed(data.n_accounts, data.opening_balance, data.currency)
    report = await engine.conservation()
    report.expected_total = data.n_accounts * data.opening_balance
    report.ok = report.total_balance == report.expected_total
    return report


@get("/admin/conservation", summary="Sum of all balances")
async def conservation(
    engine: Engine, expected_total: int | None = None
) -> ConservationReport:
    """ARCH_DESIGN.md §6.1 — the check that makes every other number trustworthy.

    Run before a load run, run after, compare. Transfers move money; they never create
    or destroy it. If this drifts, the engine is broken and its throughput is fiction.
    """
    report = await engine.conservation()
    if expected_total is not None:
        report.expected_total = expected_total
        report.ok = report.total_balance == expected_total
        if not report.ok:
            log.error(
                "conservation.violated",
                total_balance=report.total_balance,
                expected_total=expected_total,
                drift=report.total_balance - expected_total,
            )
    return report


router = Router(
    path="",
    route_handlers=[health, get_account, create_transfer, seed, conservation],
)
