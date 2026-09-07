-- Atomic fund transfer + outbox append (ARCH_DESIGN.md D4).
--
-- The XADD at the bottom is the entire point of this script. Redis runs it on one
-- thread with nothing interleaved, so the balance mutation and the log entry either
-- both happen or neither does. Without that, a sim pod crash between "write balance"
-- and "publish transfer" leaves the ledger and the log disagreeing — which is not data
-- loss, it is inconsistency, and it makes the conservation check unable to tell a real
-- bug from a crash artifact.
--
-- KEYS[1] acct:{from}   KEYS[2] acct:{to}   KEYS[3] idem:{key} ('' to skip)
-- KEYS[4] stream key
-- ARGV[1] amount        ARGV[2] transfer_id (26-char ULID)
-- ARGV[3] currency      ARGV[4] memo
-- ARGV[5] stream maxlen ARGV[6] created_at (epoch ms)
-- ARGV[7] from_id       ARGV[8] to_id       ARGV[9] idem ttl seconds
--
-- returns {status, transfer_id, created_at, from_customer_id, to_customer_id}
-- status: 0=insufficient 1=ok 2=duplicate 3=no_such_account 4=currency_mismatch
--         (keep in sync with models.TransferStatus)
-- customer ids are '' when unknown (e.g. the account does not exist).

local from_key, to_key, idem_key, stream_key = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local amount   = tonumber(ARGV[1])
local xfer_id  = ARGV[2]
local currency = ARGV[3]
local memo     = ARGV[4]
local maxlen   = tonumber(ARGV[5])
local created  = ARGV[6]
local from_id  = ARGV[7]
local to_id    = ARGV[8]
local idem_ttl = tonumber(ARGV[9])

-- Fetched once, up front, so every return path — including duplicate and
-- account-not-found — carries the same shape back to the caller.
local from_cid = redis.call('HGET', from_key, 'customer_id') or ''
local to_cid   = redis.call('HGET', to_key, 'customer_id') or ''

local claimed_idem = false

local function release()
  -- A *failed* transfer must not burn the idempotency key, or a client retrying after
  -- a transient failure gets a permanent bogus 'duplicate'.
  if claimed_idem then redis.call('DEL', idem_key) end
end

-- 1. idempotency claim. SET NX is the whole mechanism; storing the transfer id lets a
--    replay return the *original* result rather than a bare "duplicate".
if idem_key ~= '' then
  if redis.call('SET', idem_key, xfer_id, 'NX', 'EX', idem_ttl) then
    claimed_idem = true
  else
    local prior = redis.call('GET', idem_key)
    return {2, prior, created, from_cid, to_cid}
  end
end

if amount <= 0 then release(); return {0, '', created, from_cid, to_cid} end
if from_id == to_id then release(); return {0, '', created, from_cid, to_cid} end

-- 2. both accounts must exist
local from_ccy = redis.call('HGET', from_key, 'currency')
local to_ccy   = redis.call('HGET', to_key, 'currency')
if not from_ccy or not to_ccy then release(); return {3, '', created, from_cid, to_cid} end

-- 3. same-currency only (no FX — explicit non-goal)
if from_ccy ~= currency or to_ccy ~= currency then
  release(); return {4, '', created, from_cid, to_cid}
end

-- 4. funds
local avail = tonumber(redis.call('HGET', from_key, 'avail_balance'))
if avail < amount then release(); return {0, '', created, from_cid, to_cid} end

-- 5. mutate + log. Atomic from here by construction.
redis.call('HINCRBY', from_key, 'avail_balance', -amount)
redis.call('HINCRBY', from_key, 'balance', -amount)
redis.call('HINCRBY', to_key, 'avail_balance', amount)
redis.call('HINCRBY', to_key, 'balance', amount)

-- MAXLEN ~ is approximate on purpose: it trims whole nodes, which is what makes it
-- cheap enough to sit on the hot path. See ARCH_DESIGN.md D4 — this bound is relay
-- downtime tolerance, and trimming below relay lag loses transfers silently.
redis.call('XADD', stream_key, 'MAXLEN', '~', maxlen, '*',
           'id', xfer_id,
           'f',  from_id,
           't',  to_id,
           'a',  amount,
           'c',  currency,
           'm',  memo,
           'ts', created,
           'fc', from_cid,
           'tc', to_cid)

return {1, xfer_id, created, from_cid, to_cid}
