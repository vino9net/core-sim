# Benchmark results

First measured numbers for `redis_lua`, 2026-07-16, on a **13-inch M4 MacBook Air**.
**Provisional, and the hardware is the reason** — see
[Known problems](#known-problems-that-invalidate-parts-of-this).

Headline: **`redis_lua` tops out around 22k tps on this laptop**, against a README target
of 80-120k. Read that as a fact about the MacBook Air, not about the engine. This machine
has **4 performance cores** (not 10 — see below), no fan, and was running the load
generator and Redis alongside the server. It is close to the worst possible box to
benchmark on, and these numbers should not be compared to the README's 8-core-VM targets.

## Environment

| | |
|---|---|
| host | **MacBook Air, 13-inch, Apple M4** (`Mac16,12`), 24 GB, darwin/arm64 |
| cores | **10 total — 4 performance + 6 efficiency**. Not interchangeable. |
| cooling | **Fanless.** Sustained load throttles; every run here was 10-30s of full saturation. |
| server | `uvicorn` + uvloop + httptools, Litestar, `access_log=False` |
| store | Redis 7 (Homebrew, local), no persistence |
| engine | `redis_lua` |
| load | k6 v2.1.0, `bench/transfer.js`, `THETA=0` (uniform) |
| dataset | 10,000 accounts, opening balance 100,000,000 SGD |

The core topology is the single most important line in this table. `sysctl hw.ncpu`
reports 10, but only **4 are performance cores**; the other 6 are efficiency cores at a
fraction of the throughput. And all three components of the benchmark — k6, uvicorn, and
Redis — compete for those same 4:

```
4 P-cores  ÷  [ k6 (multi-threaded Go) | Redis (1 thread) | uvicorn workers ]
```

That leaves roughly **two P-cores for the server**, which is almost exactly where worker
scaling stops (below). A fanless chassis then throttles whatever survives that. Every
number here is a *relative* figure from a contended, thermally-limited laptop.

## Conservation

Checked before and after every run in this document:

```
before: {"n_accounts":10000, "total_balance":1000000000000, "ok":true}
after:  {"n_accounts":10000, "total_balance":1000000000000}
```

Exact match across 368,448 concurrent transfers at 50 VUs. **No drift.** Per
`ARCH_DESIGN.md` §6.1 that is what makes the rest of this trustworthy: the engine is not
minting or destroying money, so its throughput is a measurement rather than fiction.

## Results

### Single worker — `uv run core-sim` as documented

50 VUs, 30s, seeded:

| metric | value |
|---|---|
| throughput | **12,217/s** |
| transfer_ok | 100.00% (368,448 / 368,448) |
| http_req_failed | 0.00% |
| latency | avg 4.03ms, med 4.01ms, p90 4.33ms, p95 4.63ms, min 976µs |

`src/core_sim/__main__.py` calls `uvicorn.run()` with no `workers` argument and
`config.py` has no workers knob, so `uv run core-sim` is **one process on one core** —
and on this machine, one of only 4 that are fast. 12.2k is one P-core's ceiling, not the
rig's.

### Worker scaling — it stops at two, and the hardware says why

100 VUs, 10s, via the factory target from `__main__.py`'s docstring:

```
uv run uvicorn core_sim.app:app --factory --workers N \
  --loop uvloop --http httptools --no-access-log --port 8001
```

| workers | throughput | vs. 1 worker |
|---|---|---|
| 1 | 11,691/s | 1.00x |
| 2 | 20,648/s | 1.77x |
| 4 | 22,185/s | 1.90x |
| 8 | 20,050/s | 1.71x |

**Scaling dies after 2 workers and goes backwards at 8** — and the M4 Air's topology
predicts almost exactly that. There are 4 P-cores. Redis needs one (it is single-threaded
and was doing >20k EVALSHA/s). k6 needs at least one, realistically more. That leaves
**~2 P-cores for uvicorn**, which is precisely where the curve flattens: the jump 1→2 is
a healthy 1.77x, and everything after it is nearly free.

At 8 workers there are only 4 P-cores to hold them, so the rest land on **efficiency
cores** that run a fraction as fast — which is a coherent story for why 8 workers is
*slower* than 4. On a machine with 8 real cores and the load generator elsewhere, this
curve would very likely keep climbing.

Two further caveats make the curve's *shape* untrustworthy, and both bias later runs
downward — the same direction as the 8-worker dip:

- The runs went 1→2→4→8 against a **progressively dirtier Redis** (see below).
- They ran back-to-back on a **fanless laptop**, so later runs were the most thermally
  throttled. No cooldown between steps.

The *ceiling* around 22k reproduced across separate runs and is solid; the shape is not.

Adding VUs does not move it either — 8 workers at 200 VUs gave 22,667/s at p95 12.54ms,
versus 20,259/s at p95 3.54ms with 50 VUs. **+12% throughput for 4x the latency**, which
is what a saturated queue looks like.

### Component ceilings — where the bottleneck is *not*

Each measured in isolation on the same box:

| path | throughput | how |
|---|---|---|
| `/health`, 8 workers, no Redis | **93,205/s** | k6, 200 VUs |
| Redis `EVAL return 1` | 251,889/s | `redis-benchmark -n 100000 -c 50` |
| Redis `SET` | 236,407/s | `redis-benchmark -t set` |
| Redis, transfer's op mix (SET/HGET×3/HINCRBY×4/XADD) | **122,699/s** | `redis-benchmark eval` |
| **`/transfer` end to end** | **~22,000/s** | k6 |

This is the interesting result. The HTTP stack alone does 93k. Redis running this exact
script alone does 123k. Composed, they do 22k — **4x below the slower component**.

Note what each isolated test *doesn't* pay for. `/health` returns a dict and touches no
Redis: no body decode, no ULID, no client round trip. `redis-benchmark` is a C client and
pays no Python cost at all. Neither measures the per-request work that only exists on the
composed path, so "93k and 123k" was never a prediction of ~90k — it sets an upper bound,
not an estimate.

Candidates, roughly in order of suspicion:

- **Contention for 4 P-cores** — the composed path needs k6 *and* Redis *and* uvicorn on
  them simultaneously. `/health` frees an entire P-core by removing Redis from the path,
  which is likely a large part of why it looks so much faster.
- **Python-side `redis-py` cost per call** — encoding 4 keys + 9 args, awaiting the
  socket, parsing the reply, plus msgspec decode and `new_ulid()`. Invisible to both
  isolated tests.
- **Thermal throttling** — fanless chassis, sustained saturation.
- **Connection count** — 8 workers × `redis_max_connections=64` = up to 512 sockets into
  a single-threaded Redis. `redis-benchmark` used 50.
- **Keyspace pollution** — see below. Every measurement ran against a growing Redis.

None is confirmed. Profiling a worker would settle the Python-cost question, but the
honest first move is to **re-run on a machine with real cores and the generator on a
separate host** — several of these confounds disappear at once, and until they do this
box cannot tell us much about the engine.

## Known problems that invalidate parts of this

### The idempotency keyspace leaks, and it poisons consecutive runs

After the runs in this document:

```
dbsize:      1,797,755      (idem: 1,787,752 | acct: 10,000)
used_memory: 279.90M
xlen transfers: 1,000,042   (at STREAM_MAXLEN, trimming as designed)
```

`idem_ttl_seconds` defaults to **86400 — 24 hours** — and `bench/transfer.js` builds its
key from `Date.now()`, so every key is unique. The Lua `release()` only DELs on *failure*,
so every **successful** transfer leaks a key that outlives the run by a day. A 30s run at
12k tps leaves ~370k permanent keys behind.

Consequences:

1. **Consecutive runs are not independent.** The worker sweep above ran 1→2→4→8 against
   a Redis growing past 1.7M keys. The 8-worker dip may be nothing but "ran last."
2. **The dedupe path is never exercised.** Unique keys mean `SET NX` always succeeds,
   the 200-replay branch never fires, and `transfer_duplicates` is structurally always 0.
3. `POST /admin/seed` between runs is currently the only reset, since it UNLINKs `idem:*`.

### The 404 path is ~7.5x more expensive than the success path

The first run of the day was made against an **unseeded** Redis. Every request 404'd:

| | unseeded | seeded |
|---|---|---|
| throughput | 1,628/s | 12,217/s |
| transfer_ok | 0 / 48,904 | 368,448 / 368,448 |
| p95 | 31.56ms | 4.63ms |

The failing path does strictly *less* work — the Lua script bails at the first `HGET` and
skips four `HINCRBY`s plus the `XADD` — yet it is **7.5x slower**. The script is one round
trip either way, so Redis op count is nearly free; the difference is that `api.py` raises
`HTTPException` per failure and Litestar's error-response machinery costs roughly 614µs
against the success path's 82µs.

**This is a live hazard for the theta sweep**, which is the project's actual deliverable.
At `THETA=0.99` the hot account drains and starts returning 422 `INSUFFICIENT_FUNDS` —
down that same expensive exception path. Contention would then appear to cost throughput
partly because *Litestar exception handling is slow*, not because of engine behaviour.
Returning a normal `Response` with a status code instead of raising would remove the
confound. **Do not trust any high-theta number until this is fixed.**

### The load model is closed, so latency here means nothing

`bench/transfer.js` uses `vus: N, duration: D` — a closed loop. Every latency figure above
is Little's Law restating the VU count:

```
50 VUs ÷ 12,217/s = 4.09ms   (measured avg: 4.03ms)
```

The VUs wait for a response before sending again, so when the server slows, offered load
slows with it and the requests that *would* have been slow are never sent — textbook
coordinated omission. Real unqueued service time is visible in `min=976µs`, and in the
one unqueued request of the unseeded run (`{expected_response:true}: avg=455µs`, a single
sample — the teardown's conservation GET).

Throughput numbers survive this; **latency numbers do not**. k6's `constant-arrival-rate`
executor (open model) is the fix, and it matters most for the theta sweep, where the
closed loop will *understate* how badly `pg_naive` falls over.

### `bench/transfer.js`'s check passes when nothing works

```js
check(res, { "no server error": (r) => r.status < 500 });
```

The unseeded run reported `checks_succeeded: 100.00%` and `✓ no server error` while
achieving **zero** successful transfers. `404 < 500` is true — and so is `0 < 500`, k6's
code for connection refused or timeout, so this check also passes against a server that
is entirely down. Only the custom `transfer_ok` metric and the `http_req_failed` threshold
caught the failure.

## Reproducing

k6 is **not** a project dependency and is not declared anywhere; install it separately
(`brew install k6`). `bench/transfer.js` is not Node — `k6/http` and `k6/metrics` are the
k6 runtime's own built-ins, so there is nothing to `npm install`.

```bash
brew services start redis
uv run core-sim

# REQUIRED. Without it every transfer 404s and the run measures the error path.
curl -XPOST localhost:8000/admin/seed -H 'content-type: application/json' \
  -d '{"n_accounts": 10000, "opening_balance": 100000000, "currency": "SGD"}'

k6 run -e THETA=0 -e VUS=50 bench/transfer.js
```

Reseed between runs — it is the only thing that clears the leaked `idem:*` keyspace.

Bracket every run with the conservation check and compare against the `expected_total`
that `/admin/seed` prints. Note that the k6 teardown calls `/admin/conservation` *without*
`?expected_total=`, so it prints `"ok":null` and cannot self-verify; the comparison is
currently manual.

## Open questions

1. **Re-run this on real hardware** — an 8-core VM with the load generator on a separate
   host. Everything below is downstream of that; the M4 Air confounds core count, core
   *class*, thermals, and generator contention all at once, and no amount of care on this
   box separates them.
2. Where do the missing ~70k tps go? Profile a worker to size the Python-side `redis-py`
   and msgspec cost, which neither isolated ceiling test captures.
3. Does worker scaling still stop at 2 once there are more than 4 fast cores and k6 is
   elsewhere? Reseed between every step this time.
4. Does the exception-path cost fully explain the 404 path's 7.5x, or is something else
   on the failure path?
5. `pg_naive` was previously "measured 3-5k" — on what hardware? If it was a machine like
   this one, the whole comparison matrix needs recalibrating before the engines can be
   ranked against each other.
