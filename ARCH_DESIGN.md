# Core Banking Simulator — Architecture & Design Decisions

Status: design agreed, not yet implemented
Last updated: 2026-07-15

## 1. Scope

A simulator of exactly one core banking function: **fund transfer between accounts**.

It exists to be a **load-test target / benchmark rig**. The product is not the API — it is
the *comparison* between storage engines under varying contention. A number without
something to contrast it against is not useful output.

Everything below follows from that. Several decisions would be wrong for a real ledger and
are correct here.

## 2. Goals and non-goals

**Goals**

- Maximise transfer throughput, single process and scaled horizontally.
- Model contention explicitly: uniform-random vs hot-account access patterns.
- Be honest: every engine must pass a conservation check (see §6.1). Wrong-but-fast is
  the easiest thing in the world to accidentally build.

**Non-goals**

- Durability of *balances*. Balances are derived state and are rebuilt by replay (§6.3).
- Double-entry bookkeeping. Simple balance mutation only. (This is what rules out
  TigerBeetle, which would otherwise be the throughput winner — it forces double-entry.)
- FX / multi-currency conversion. Same-currency transfers only.
- Being a real bank.

## 3. Decisions

### D1 — Python 3.14, standard (GIL) build

**Why:** The workload is I/O-bound — every request is waiting on Redis. asyncio plus one
process per core gets full throughput with no ecosystem risk.

**Rejected — free-threaded build (3.14t):** Free-threading is officially supported as of
3.14, but it buys nothing for an I/O-bound service and costs C-extension compatibility.
Multiple processes were already the scale-out story.

### D2 — Litestar, on Granian or uvicorn+uvloop

**Why:** ~2x FastAPI's request handling at comparable ergonomics; msgspec serialization is
meaningfully faster than Pydantic v2.

**Honest caveat:** the store dominates. Framework choice is worth single-digit percent
here. FastAPI would be fine. This is a cheap win, not a load-bearing decision.

### D3 — Redis as the ledger store

**Why:** Redis executes commands on a single thread, so a Lua script is atomic by
construction. No locks, no lock ordering, no deadlock avoidance, no `SELECT ... FOR UPDATE`.

The deeper reason: **Redis is a single-writer actor that happens to live in another
process.** Hot-account contention cannot exist, because contention requires two writers and
there is only ever one. Uniform and hot-account throughput are *the same number*.

**Accepted costs — both are real:**

1. **It deletes the contention experiment.** The hot-account cliff is structurally
   impossible in Redis. This is why Postgres engines stay in the matrix (§3.7) — otherwise
   we lose the most interesting result we set out to produce.
2. **It cannot be sharded.** Redis Cluster has no atomic cross-slot operations, and a
   transfer touches an arbitrary *pair* of accounts. Hash tags can co-locate specific
   accounts but not all pairs when anyone can pay anyone. **One instance, one core for
   command execution, forever.** High ceiling (~100-200k tps) but a ceiling, not a slope.
   Postgres burns multiple cores and would keep climbing on a large box; they converge
   somewhere around 32-64 cores.

**Persistence: off.** `appendonly no`, `save ""`. See §6.3 — the log is the durable thing.

**Connection pool: `BlockingConnectionPool`, not the default.** Found during scaffolding.
redis-py's default `ConnectionPool` *raises* `MaxConnectionsError` the instant concurrent
requests exceed `max_connections` — under load that surfaces as spurious 500s that read
like Redis failing. Excess concurrency must **queue** (backpressure), so the wait lands
honestly in p99 rather than as a fake error. This is not incidental: it is the direct
consequence of §7.2 wanting *high* `containerConcurrency` for batching, which means large
request bursts per pod. Sizing note — Redis executes on one thread, so a deeper pool buys
no throughput; it only needs to keep the socket busy, past which it is pure queueing.

**Dragonfly:** Redis-protocol-compatible and genuinely multi-threaded, so it *can* scale
with cores. Kept as a config-only swap (connection string). Note it needs ~4+ cores to beat
Redis; on a small box it is likely *worse*, since you pay coordination overhead for
parallelism you don't have. **See §8 — this swap is not yet proven free.**

### D4 — Transactional outbox: `XADD` inside the transfer script

The ledger mutation and the log entry are **one atomic Lua script**:

```lua
-- KEYS[1]=from KEYS[2]=to KEYS[3]=idem
if redis.call('SET', KEYS[3], 1, 'NX', 'EX', 300) == false then return 2 end  -- idempotent replay
local avail = tonumber(redis.call('HGET', KEYS[1], 'avail'))
if avail < amount then return 0 end
redis.call('HINCRBY', KEYS[1], 'avail', -amount)
redis.call('HINCRBY', KEYS[2], 'balance', amount)
redis.call('XADD', 'transfers', 'MAXLEN', '~', 1000000, '*', ...)
return 1
```

**Why:** Without this, a sim pod does "write Redis, then publish to the queue" — two systems,
no atomicity. A crash in between leaves balance moved with no record. That is not data loss,
it is **inconsistency**: the conservation check then fails and you cannot tell a real bug from
a crash artifact.

**This is the decision that makes everything else work.** Because the log entry is written
atomically with the balance, the sim pod holds *no state*: every request is either not yet in
Redis (it never happened; the client retries on the idempotency key) or fully in Redis
including its log entry (durable, the relay will ship it). There is no in-between to lose.
Sim pods become genuinely disposable — which is what makes both HPA and Knative viable.

**`MAXLEN ~ 1000000` is the one knob that can still lose data.** The stream is the relay's
buffer, so its length is relay-downtime tolerance. At 100k tps that is ~10s of runway and
~100MB RAM; 10M gives ~100s for ~1GB. **Trim below relay lag and transfers vanish with no
error anywhere.** Set it deliberately.

### D5 — Relay pod: Redis Streams → NATS JetStream

~25 lines, written in-house (§8 notes the off-the-shelf option). Consumer group,
`XREADGROUP` → batch → `js.publish()` → `XACK`.

**Publish *then* `XACK` is the whole durability argument.** Crash in between and entries stay
in the Pending Entries List; the restarted relay re-sends. At-least-once; consumers dedupe on
transfer id.

**Why two streams — they are not redundant, they do different jobs:**

- **Redis stream = outbox.** Exists *only* because it can be written atomically with the
  ledger. In-memory, bounded to seconds, dies with Redis. It was never the log.
- **NATS JetStream = log of record.** Durable, replayable, what consumers subscribe to — so
  they aren't coupled to the ledger store or adding load to Redis's single bottleneck thread.

Collapsing either one breaks something: drop the Redis stream and the sim pod is back to two
non-atomic writes; drop NATS and there is no durable log to replay balances from.

**Adaptive batching comes free.** `XREADGROUP count=1000 block=100`: under load, full batches
immediately (ack cost amortizes to ~nothing); when idle, small batches at low latency. No
timer, no flush logic. At 1000 transfers/message, 100k tps is ~100 publishes/sec at ~300µs —
a 3% duty cycle on one asyncio loop.

**Gotcha — drain the PEL on startup.** Read with cursor `"0"` until empty, *then* switch to
`">"`. Starting at `">"` skips your own pending entries and they sit in the PEL forever —
silently, and only after a restart. (`XAUTOCLAIM` is only needed if running >1 relay.)

**Rejected — Kafka:** ecosystem, partition ordering at scale, and EOS buy a simulator
nothing, and the bill is 3 JVM brokers + PVCs + an operator. That would cost more nodes than
Redis and the sim pods combined. NATS JetStream R1 is one Go binary, one pod, one PVC. R3 if
NATS itself needs to survive a pod loss — still well under a Kafka install.

### D6 — Representation and correctness

| Decision | Rationale |
|---|---|
| Money as `BIGINT` minor units | Never `NUMERIC` (slow), never float (wrong) |
| Idempotency key, `SET NX` in-script | Retries are safe. **The load generator *will* retry.** Without this, a retry double-spends |
| Fixed-width 64-byte binary log records | `struct.pack` ~0.2µs vs ~5µs stdlib JSON. At 100k tps JSON shows up in the profile. `np.fromfile` makes analysis trivial |
| Transfer id (ULID/snowflake) on every record | Consumer-side dedupe for at-least-once delivery |

### D7 — Engine matrix (this is the deliverable)

Same REST surface, swappable by config. **The comparison is the product.**

| Engine | Uniform | Hot account | Notes |
|---|---|---|---|
| `pg_naive` | ~5k | ~300 | Baseline. **Measured at 3-5k in prototype** — this is the calibration fixed point |
| `pg_sproc` | ~30k | ~1.5k | One round trip, ordered `FOR UPDATE`. Collapses lock hold time |
| `pg_sharded` | ~35k | ~20k | Balance sharded across N rows; turns the cliff into a slope |
| `pg_batched` | ~50k | ~50k | In-process netting + batch apply |
| `redis_lua` | ~80-120k | ~80-120k | One `EVALSHA` |
| `redis_batched` | ~300-500k | ~300-500k | N transfers per script |
| `dragonfly_batched` | measure | measure | Config swap — see §8 |

All figures are estimates for an 8-core VM except the measured `pg_naive` row.

**The recurring lesson, and the rig should demonstrate it:** every optimisation here is the
same one. `pg_batched`, `redis_batched`, the NATS publish, the file flush, surviving K8s
cross-node RTT — all of it is *amortize the call*. The call is always the cost.

**Keep the Postgres engines.** They are where the hot-account story lives, and `pg_naive` is
the only measured point to calibrate everything else against.

### D8 — Load generation

**k6** (or `oha`/`vegeta`), **not Python.** A Python load generator falls over well before the
server does; you spend a day benchmarking your benchmarker.

Account selection uses a **Zipf `theta` parameter**: 0 = uniform, ~0.99 = brutally hot. This
knob *is* the contention experiment — the resulting curve per engine is the artifact worth
having.

## 4. Architecture

```
                      ┌──────────────────────────────┐
   k6 ──HTTP──►       │  sim pods (N, stateless)     │
                      │  Litestar + redis.asyncio    │
                      └──────────────┬───────────────┘
                                     │ EVALSHA: debit + credit + XADD (atomic)
                                     ▼
                      ┌──────────────────────────────┐
                      │  Redis / Dragonfly  (1 pod)  │
                      │  no persistence              │
                      │  stream `transfers` = outbox │
                      └──────────────┬───────────────┘
                                     │ XREADGROUP (consumer group)
                                     ▼
                      ┌──────────────────────────────┐
                      │  relay (1 pod, NOT Knative)  │
                      │  publish → XACK              │
                      └──────────────┬───────────────┘
                                     ▼
                      ┌──────────────────────────────┐
                      │  NATS JetStream — log of record
                      │  → consumers, replay, conservation check
                      └──────────────────────────────┘
```

## 5. Data model

Redis, per account (`acct:{id}` hash):

| Field | Type | Notes |
|---|---|---|
| `account_no` | string | |
| `currency` | string | ISO 4217 |
| `balance` | int | minor units |
| `avail` | int | minor units |
| `status` | int | |

Transfer record (64-byte fixed-width binary, in the stream and on to NATS): `id`,
`from_account`, `to_account`, `amount`, `currency`, `ts`, `status`.

Postgres engines mirror this: `accounts` and `transfers` as `UNLOGGED` tables (skips WAL,
truncated on crash recovery — correct for a simulator, worth ~2-3x), plus
`synchronous_commit=off`, plus `account_balance_shards` for `pg_sharded`.

## 6. Durability and failure modes

### 6.1 Conservation check — non-negotiable

Sum every balance before and after a run. **It must be identical.** Transfers move money;
they never create or destroy it. If the sum drifts, the engine is broken and its throughput
number is fiction.

Every engine except `pg_naive` and `redis_lua` is doing something clever enough to get this
wrong. This check is what makes the rig trustworthy.

*(Context: the earlier Redis prototype measured slower than Postgres. Likely a sync client
blocking the event loop, and/or read-modify-write without Lua — which would have been a
lost-update race, i.e. benchmarking a broken implementation. This check would have caught it.)*

### 6.2 What happens when each thing dies

| Dies | Impact | Mitigation |
|---|---|---|
| **Sim pod** | In-flight requests reset. They never took effect (D4) | Client retries on idempotency key. **No loss, no inconsistency.** Nothing to drain |
| **Relay pod** | Nothing lost — entries stay in the PEL | Restart resumes from PEL. Stream is the buffer; sized by `MAXLEN` |
| **Redis** | All balances *and* the stream — but **lost consistently** | Reseed + replay from JetStream (§6.3) |
| **NATS** | The durable log | R3 stream if this matters |

**RPO = relay lag**, and nothing else. Keep the loop tight and that is single-digit ms —
better than Redis `appendfsync everysec` would give, at ~zero hot-path cost, because all the
durability work happens on a pod that isn't serving requests.

### 6.3 Recovery: balances are derived

`balances = seed + replay(transfers)`. Redis is a fast materialized view, not the source of
truth. This is why it needs no persistence — put the durability effort into the *log*, not
the ledger. Standard event sourcing.

**Reseed + replay must be a first-class endpoint**, not a script in someone's shell history.
It will be run constantly.

### 6.4 Rejected durability options

| Option | Why not |
|---|---|
| `appendfsync always` | ~1-5k ops/sec. Throws away the entire reason for choosing Redis |
| `appendfsync everysec` + StatefulSet + PVC | ~10-30% off the single bottleneck thread, to beat an RPO that replay already gives us for free |
| Redis replica + Sentinel | A pod plus real complexity; replay covers it |
| `emptyDir` + RDB | The cheap middle ground *if ever wanted*: survives container restart (OOMKill) without a PVC, and RDB is cheap here because state is small (~50MB for 1M accounts) even though the write rate is huge. Not needed given §6.3 |
| Deployment + `emptyDir` + AOF | **Theater.** `emptyDir` does not survive reschedule. Durability across pod moves requires a StatefulSet and a real PVC |

## 7. Deployment

### 7.1 K8s baseline

| Component | Workload type | Notes |
|---|---|---|
| sim pods | Knative Service (§7.2) | Stateless. Scale freely |
| Redis / Dragonfly | Deployment + Service | No PVC, no persistence. Reseed on restart |
| relay | **plain Deployment** | **Not Knative** — see §7.2 |
| NATS | StatefulSet + PVC | JetStream R1 (R3 if NATS must survive pod loss) |

**Pod-to-pod RTT is the gotcha that will actually bite.** Same-node ~0.05ms, cross-node
~0.2-0.5ms. A Redis round trip is ~50-100µs of *work* wrapped in that — a cross-node hop can
quintuple it, and one connection at 0.3ms RTT does ~3.3k/s, full stop. Land sim pods on a
different node from Redis and the 100k evaporates for reasons unrelated to Redis.

Batching rescues this too (same lesson as D7): 100+ transfers per `EVALSHA` → ~1000 round
trips/sec → trivial even at 0.5ms.

*Note: Redis tolerating thousands of connections is a real advantage over Postgres under
churn-heavy autoscaling. Postgres would need PgBouncer and would still struggle.*

### 7.2 Knative — three consequences, one of them a blocker

**⚠ The relay cannot be a Knative Service.** It is a background loop, not request-driven.
Knative Serving would scale it to zero when no HTTP traffic arrives — and relaying silently
stops, which breaks durability. Run it as a **plain Deployment**. (`min-scale: 1` would pin
it, but Knative Serving is simply the wrong abstraction for a worker; don't fight it.)
Mixing Knative Services and plain Deployments in one namespace is fine and normal.

**⚠ Knative's scaling model fights in-process batching.** Batching needs *many concurrent
requests per pod*. If `containerConcurrency` is low, KPA spreads load across many small pods
and each has too few in-flight requests to form a batch — `redis_batched` collapses toward
`redis_lua` and the headline number is lost. **Set `containerConcurrency` high (or 0) and
control pod count via `min-scale`/`max-scale` instead.** Few pods with high concurrency, not
many pods with low.

**⚠ The queue-proxy sidecar is now inside your measurement.** Every request traverses
Knative's queue-proxy before reaching the app — ~0.1-0.5ms and real CPU. For a rig measuring
microseconds, **you are benchmarking Knative + app, not app.** This must be disclosed
alongside any published number, and it is a strong argument for also running the matrix on a
plain Deployment to isolate the Knative tax. That delta is itself an interesting result.

**Also:** pin `min-scale` ≥ 1 during runs. Scale-to-zero cold starts and connection-pool
re-establishment will pollute results.

**The good news:** sim pods fit Knative *because of D4*. Holding no state is exactly what
Knative's aggressive churn demands. The architecture we landed on is the one Knative needs —
that alignment is not a coincidence, it's the same property (statelessness) paying out twice.

## 8. Open questions / to verify before building

1. **Dragonfly Streams + consumer groups.** The whole outbox leans on `XADD`, `XREADGROUP`,
   `XACK`, `XAUTOCLAIM`. Dragonfly implements Streams, but consumer-group behaviour under
   load should be **confirmed by a ~20-minute spike, not taken on faith**. If support is
   thinner than advertised, "swap Dragonfly by config" quietly stops being free — much better
   to learn now than after the engine interface is built around it.
2. **Knative tax.** Measure the plain-Deployment vs Knative-Service delta before trusting any
   absolute number.
3. **Relay: build vs Redpanda Connect.** Redpanda Connect (ex-Benthos) does
   `redis_streams` → `nats_jetstream` in config, single Go binary, no code — a legitimate
   choice. Decided to write the 25 lines because the relay is a *measured component*: we want
   our own lag/batch-size/ack-latency instrumentation, the binary record format, and a loop
   we can read when a run looks weird. Revisit if the relay stops being interesting.
4. **JetStream R1 vs R3.** R1 until NATS pod loss is shown to matter.
