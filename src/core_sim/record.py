"""Fixed-width 160-byte binary transfer record (ARCH_DESIGN.md D6).

Why not JSON: ``struct.pack`` is ~0.2µs vs ~5µs for stdlib json. At 100k tps that
difference shows up in the profile of a rig whose entire job is measuring µs. It also
makes analysis trivial::

    import numpy as np
    a = np.fromfile("transfers.bin", dtype=DTYPE)
    assert a["amount"].sum() == 0   # if you log signed legs

This is still one record per transfer, not one per ledger leg — a consumer that wants
a debit-account / credit-account view (e.g. the DynamoDB projection in the downstream
consumer) fans this one record out into two, since only the consumer needs that shape.

Layout (little-endian, no padding — sizes add to exactly 176)::

    16s   id                 ULID, binary
    Q     from_account       uint64
    Q     to_account         uint64
    q     amount             int64, minor units
    4s    currency           3 chars + 1 pad
    q     created_at         int64, epoch ms
    B     status             uint8
    100s  memo               UTF-8, null-padded/truncated to MEMO_SIZE bytes
    Q     from_customer_id   uint64
    Q     to_customer_id     uint64
    7x    reserved
"""

from __future__ import annotations

import os
import struct
import time

MEMO_SIZE = 100

_RECORD = struct.Struct(f"<16sQQq4sqB{MEMO_SIZE}sQQ7x")
RECORD_SIZE = _RECORD.size
assert RECORD_SIZE == 176, f"record must be 176 bytes, got {RECORD_SIZE}"

# numpy dtype for offline analysis; mirrors the struct above.
NUMPY_DTYPE = [
    ("id", "S16"),
    ("from_account", "<u8"),
    ("to_account", "<u8"),
    ("amount", "<i8"),
    ("currency", "S4"),
    ("created_at", "<i8"),
    ("status", "u1"),
    ("memo", f"S{MEMO_SIZE}"),
    ("from_customer_id", "<u8"),
    ("to_customer_id", "<u8"),
    ("_pad", "S7"),
]

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> bytes:
    """16-byte ULID: 48-bit ms timestamp + 80 bits of randomness.

    Hand-rolled rather than a dependency — it is five lines and we only need
    lexicographic-by-time ordering and uniqueness.
    """
    return int(time.time() * 1000).to_bytes(6, "big") + os.urandom(10)


def ulid_to_str(raw: bytes) -> str:
    """Crockford base32, 26 chars — the canonical ULID text form."""
    n = int.from_bytes(raw, "big")
    out = bytearray(26)
    for i in range(25, -1, -1):
        out[i] = ord(_CROCKFORD[n & 0x1F])
        n >>= 5
    return out.decode("ascii")


def str_to_ulid(s: str) -> bytes:
    n = 0
    for ch in s:
        n = (n << 5) | _CROCKFORD.index(ch.upper())
    return n.to_bytes(16, "big")


def pack(
    ulid: bytes,
    from_account: int,
    to_account: int,
    amount: int,
    currency: str,
    created_at: int,
    status: int,
    memo: str = "",
    from_customer_id: int = 0,
    to_customer_id: int = 0,
) -> bytes:
    return _RECORD.pack(
        ulid,
        from_account,
        to_account,
        amount,
        currency.encode("ascii")[:4],
        created_at,
        status,
        memo.encode("utf-8")[:MEMO_SIZE],
        from_customer_id,
        to_customer_id,
    )


def unpack(
    buf: bytes, offset: int = 0
) -> tuple[bytes, int, int, int, str, int, int, str, int, int]:
    ulid, frm, to, amt, ccy, ts, status, memo, from_cid, to_cid = _RECORD.unpack_from(
        buf, offset
    )
    return (
        ulid,
        frm,
        to,
        amt,
        ccy.rstrip(b"\x00").decode("ascii"),
        ts,
        status,
        memo.rstrip(b"\x00").decode("utf-8", errors="replace"),
        from_cid,
        to_cid,
    )


def iter_records(buf: bytes):
    """Walk a packed batch. Batches are just records concatenated — no framing, since
    the fixed width *is* the framing."""
    for off in range(0, len(buf) - RECORD_SIZE + 1, RECORD_SIZE):
        yield unpack(buf, off)
