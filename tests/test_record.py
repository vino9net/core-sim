"""Record format is a wire contract between the Lua script, the relay, and any offline
analysis. Round-tripping it is cheap insurance against a silent layout drift."""

from __future__ import annotations

from core_sim.record import (
    RECORD_SIZE,
    iter_records,
    new_ulid,
    pack,
    str_to_ulid,
    ulid_to_str,
    unpack,
)


def test_record_is_176_bytes():
    rec = pack(new_ulid(), 1, 2, 100, "SGD", 1_700_000_000_000, 1)
    assert len(rec) == RECORD_SIZE == 176


def test_round_trip():
    ulid = new_ulid()
    rec = pack(
        ulid,
        42,
        99,
        -12345,
        "USD",
        1_700_000_000_000,
        1,
        memo="rent",
        from_customer_id=7,
        to_customer_id=11,
    )
    got_ulid, frm, to, amt, ccy, ts, status, memo, from_cid, to_cid = unpack(rec)
    assert got_ulid == ulid
    assert (frm, to, amt, ccy, ts, status, memo, from_cid, to_cid) == (
        42,
        99,
        -12345,
        "USD",
        1_700_000_000_000,
        1,
        "rent",
        7,
        11,
    )


def test_customer_id_defaults_to_zero():
    rec = pack(new_ulid(), 1, 2, 100, "SGD", 1_700_000_000_000, 1)
    assert unpack(rec)[8:10] == (0, 0)


def test_memo_defaults_empty_and_truncates():
    rec = pack(new_ulid(), 1, 2, 100, "SGD", 1_700_000_000_000, 1)
    assert unpack(rec)[7] == ""

    long_memo = "x" * 200
    rec = pack(new_ulid(), 1, 2, 100, "SGD", 1_700_000_000_000, 1, memo=long_memo)
    assert unpack(rec)[7] == "x" * 100


def test_ulid_text_round_trip():
    ulid = new_ulid()
    text = ulid_to_str(ulid)
    assert len(text) == 26
    assert str_to_ulid(text) == ulid


def test_ulid_is_time_ordered():
    # The relay and any replay rely on this for a sane ordering by id.
    a, b = new_ulid(), new_ulid()
    assert a[:6] <= b[:6]


def test_batch_iteration():
    # Batches are records concatenated with no framing — the fixed width is the framing.
    batch = b"".join(
        pack(new_ulid(), i, i + 1, 10 * i, "SGD", 1_700_000_000_000 + i, 1)
        for i in range(5)
    )
    assert len(batch) == 5 * RECORD_SIZE
    out = list(iter_records(batch))
    assert len(out) == 5
    assert [r[1] for r in out] == [0, 1, 2, 3, 4]
    assert [r[3] for r in out] == [0, 10, 20, 30, 40]
