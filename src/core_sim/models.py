"""Wire and domain models.

msgspec Structs, not Pydantic — Litestar serialises them natively and it is the faster
path (ARCH_DESIGN.md D2).

Money is ``int`` minor units everywhere. Never float, never Decimal (D6).
"""

from __future__ import annotations

from enum import IntEnum
from typing import Annotated

import msgspec

# Matches the memo field width in record.py (MEMO_SIZE) — kept in sync so a request
# that passes validation always fits the binary record with no silent truncation.
MEMO_MAX_LEN = 100


class TransferStatus(IntEnum):
    """Return codes from the transfer script. Values are part of the Lua contract —
    keep in sync with engines/lua/transfer.lua."""

    INSUFFICIENT_FUNDS = 0
    OK = 1
    DUPLICATE = 2
    ACCOUNT_NOT_FOUND = 3
    CURRENCY_MISMATCH = 4


# Mapped to HTTP in api.py. DUPLICATE is a 200, not an error: an idempotent replay
# succeeded the first time and the client is entitled to the original result.
STATUS_HTTP = {
    TransferStatus.OK: 201,
    TransferStatus.DUPLICATE: 200,
    TransferStatus.INSUFFICIENT_FUNDS: 422,
    TransferStatus.ACCOUNT_NOT_FOUND: 404,
    TransferStatus.CURRENCY_MISMATCH: 422,
}


class Account(msgspec.Struct):
    account_id: int
    account_no: str
    currency: str
    balance: int
    avail_balance: int
    customer_id: int = 0
    status: int = 1


class TransferRequest(msgspec.Struct):
    from_account: int
    to_account: int
    amount: int
    currency: str
    memo: Annotated[str, msgspec.Meta(max_length=MEMO_MAX_LEN)] = ""
    # Optional, but the load generator should always send one. Without it a retry
    # double-spends, and the load generator *will* retry (ARCH_DESIGN.md D6).
    idempotency_key: str | None = None


class Transfer(msgspec.Struct):
    id: str
    from_account: int
    to_account: int
    amount: int
    currency: str
    memo: str
    status: str
    created_at: int
    from_customer_id: int = 0
    to_customer_id: int = 0


class TransferResult(msgspec.Struct):
    """Internal engine result. api.py turns this into a Transfer + HTTP status."""

    status: TransferStatus
    transfer_id: str | None = None
    created_at: int = 0
    from_customer_id: int = 0
    to_customer_id: int = 0


class SeedRequest(msgspec.Struct):
    n_accounts: int = 10_000
    opening_balance: int = 1_000_000_00
    currency: str = "SGD"


class ConservationReport(msgspec.Struct):
    """ARCH_DESIGN.md §6.1. ``ok`` false means the engine is broken and any throughput
    number it produced is fiction."""

    n_accounts: int
    total_balance: int
    expected_total: int | None = None
    ok: bool | None = None
