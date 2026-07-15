# core-sim

A core banking simulator with exactly one function: **fund transfer between accounts**.

It exists to be a **load-test target / benchmark rig**. The deliverable is not the API —
it's the *comparison* between storage engines under varying contention. A throughput
number with nothing to contrast it against isn't useful output.

See **[ARCH_DESIGN.md](ARCH_DESIGN.md)** for the decisions and, more usefully, the
rejected alternatives.

## Quickstart

Needs a Redis. Either works:

```bash
brew services start redis     # local, or...
docker compose up -d redis    # ...containerised (compose also has nats + dragonfly)
```

```bash
uv sync
uv run core-sim                        # :8000

# seed, then transfer
curl -XPOST localhost:8000/admin/seed \
  -H 'content-type: application/json' \
  -d '{"n_accounts": 10000, "opening_balance": 100000000, "currency": "SGD"}'

curl -XPOST localhost:8000/transfer \
  -H 'content-type: application/json' \
  -d '{"from_account":1,"to_account":2,"amount":500,"currency":"SGD",
       "memo":"lunch","idempotency_key":"demo-1"}'

curl localhost:8000/accounts/1
```

`POST /admin/seed` is destructive, but **scoped** — it UNLINKs only `acct:*`, `idem:*`
and the outbox stream, never `FLUSHDB`. Safe to point at a Redis you share with
something else (e.g. a local Homebrew instance on db 0).

Relay (Redis outbox → NATS JetStream), in a second shell. This one does need NATS:

```bash
docker compose up -d nats
uv run core-sim-relay
```

## API

| | |
|---|---|
| `GET /accounts/{id}` | account detail incl. balance |
| `POST /transfer` | create a transfer → detail incl. id |
| `POST /admin/seed` | reseed from scratch (destructive) |
| `GET /admin/conservation?expected_total=N` | sum of all balances |
| `GET /health` | liveness |

`POST /transfer` returns **201** on success, **200** on an idempotent replay (with the
*original* transfer id), 422 for insufficient funds / currency mismatch, 404 for an
unknown account.

Always send `idempotency_key`. Without it a retry double-spends, and your load generator
*will* retry.

## Benchmarking

```bash
k6 run -e THETA=0    -e VUS=50 bench/transfer.js   # uniform random
k6 run -e THETA=0.99 -e VUS=50 bench/transfer.js   # hot account
```

`THETA` is the experiment: 0 = uniform, ~0.99 = brutally hot. Sweeping it per engine
produces the curve that is the actual point of this project.

**Always bracket a run with the conservation check.** Sum every balance before and
after — it must be identical. Transfers move money; they never create or destroy it. If
it drifts, the engine is broken and its throughput number is fiction. Every engine
except `pg_naive` and `redis_lua` is doing something clever enough to get this wrong.

## Engines

Selected by `ENGINE=`. Expected shape on an 8-core VM:

| engine | uniform | hot account | status |
|---|---|---|---|
| `pg_naive` | ~5k | ~300 | planned — **measured 3-5k**, the only calibrated point |
| `pg_sproc` | ~30k | ~1.5k | planned |
| `pg_sharded` | ~35k | ~20k | planned |
| `pg_batched` | ~50k | ~50k | planned |
| `redis_lua` | ~80-120k | ~80-120k | **implemented** |
| `redis_batched` | ~300-500k | ~300-500k | planned — the headline number |
| `dragonfly_lua` | measure | measure | config swap (`REDIS_URL=redis://localhost:6380`) |

Everything but the `pg_naive` row is an estimate until measured.

Redis is flat across `THETA` because it executes commands on one thread — it's a
single-writer actor that happens to live in another process, so contention is
*structurally impossible*. That also means it **deletes the contention experiment**,
which is why the `pg_*` engines stay in the matrix: that's where the hot-account story
lives.

## Configuration

All env vars, so the store and engine swap between runs without a rebuild. See
`src/core_sim/config.py`. The ones that matter:

| var | default | note |
|---|---|---|
| `ENGINE` | `redis_lua` | |
| `REDIS_URL` | `redis://localhost:6379` | `:6380` for Dragonfly via compose |
| `STREAM_MAXLEN` | `1000000` | **the one knob that can still lose data** — see below |
| `LOG_REQUESTS` | `false` | ~10-50µs/req; never `true` during a run |
| `LOG_JSON` | `true` | structured to stdout |

`STREAM_MAXLEN` is the relay's buffer, so it's relay-downtime tolerance. ~1M ≈ 10s @
100k tps ≈ 100MB. Trim below relay lag and transfers vanish with no error anywhere.

## Architecture in one breath

```
sim pods (stateless) ──EVALSHA: debit+credit+XADD (atomic)──► Redis ──XREADGROUP──► relay ──► NATS JetStream
```

The `XADD` inside the transfer script is the decision everything else rests on. Because
the log entry is written atomically with the balance, a sim pod holds **no state** —
every request either never happened or is fully durable, with no in-between. That's what
makes the pods disposable, which is what makes both HPA and Knative viable, and it's why
"minimize loss on pod crash" mostly dissolves as a question.

Balances are *derived*: `balances = seed + replay(transfers)`. So Redis needs no
persistence — the durability effort goes into the log, not the ledger. RPO = relay lag.

## Layout

```
src/core_sim/
  config.py      env settings
  logging.py     structlog → stdout
  models.py      msgspec structs, TransferStatus
  record.py      64-byte fixed-width binary record
  api.py         routes
  app.py         Litestar factory
  relay.py       Redis Stream → NATS JetStream
  engines/
    __init__.py  Engine ABC + registry
    redis_lua.py the implemented engine
    lua/transfer.lua
bench/transfer.js       k6, YCSB Zipf
deploy/knative/         sim Service (see the containerConcurrency comments)
deploy/k8s/             relay Deployment (NOT Knative — it's a worker) + redis
```

## Tests

```bash
uv run pytest              # record/config tests always; transfer tests need redis
```

`tests/test_transfer.py::test_conservation_under_concurrency` is the important one — it's
the test that catches a read-modify-write implementation silently minting money.

## Dev tooling

Tooling config (dev deps, ruff, ty, pre-commit) follows the house setup from
`personal/expense_tracker`, so this project lints and type-checks the same way as the
rest of the repos.

```bash
uv sync --all-extras
uv run pre-commit install       # once
uv run pre-commit run --all-files
```

Hooks: `check-merge-conflict`, `end-of-file-fixer`, `check-toml`, `ruff-check --fix`,
`ruff-format`, `ty`.

```bash
uv run ruff check . && uv run ruff format .
uv run ty check
```
