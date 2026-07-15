// k6 load driver — ARCH_DESIGN.md D8.
//
// k6, not Python: a Python load generator falls over well before the server does and
// you spend a day benchmarking your benchmarker.
//
// THETA is the contention experiment. 0 = uniform random, ~0.99 = brutally hot.
// Sweeping it per engine produces the curve that is the actual deliverable.
//
//   k6 run -e THETA=0    -e VUS=50 bench/transfer.js   # uniform
//   k6 run -e THETA=0.99 -e VUS=50 bench/transfer.js   # hot account
//
// Expect redis_* to be flat across theta (single writer => contention is structurally
// impossible) and pg_naive/pg_sproc to fall off a cliff. That contrast is the point.

import http from "k6/http";
import { check } from "k6";
import { Counter, Rate } from "k6/metrics";
import { randomSeed } from "k6";

const BASE = __ENV.BASE || "http://localhost:8000";
const N_ACCOUNTS = parseInt(__ENV.N_ACCOUNTS || "10000");
const THETA = parseFloat(__ENV.THETA || "0");
const VUS = parseInt(__ENV.VUS || "50");
const DURATION = __ENV.DURATION || "30s";
const AMOUNT = parseInt(__ENV.AMOUNT || "100");
const CURRENCY = __ENV.CURRENCY || "SGD";

const insufficient = new Counter("transfer_insufficient_funds");
const duplicates = new Counter("transfer_duplicates");
const okRate = new Rate("transfer_ok");

export const options = {
  vus: VUS,
  duration: DURATION,
  thresholds: {
    // Deliberately loose: insufficient-funds is a legitimate outcome under a hot
    // distribution, not a failure. Only 5xx and timeouts are real errors.
    http_req_failed: ["rate<0.05"],
  },
};

// --- Zipfian generator (YCSB / Gray et al. rejection-inversion) ------------------
//
// The naive `Math.pow(Math.random(), k)` trick is not a Zipf distribution and would
// misreport contention. This is the standard algorithm, so numbers are comparable to
// YCSB-based work.

function zeta(n, theta) {
  let sum = 0;
  for (let i = 1; i <= n; i++) sum += 1 / Math.pow(i, theta);
  return sum;
}

// zeta(N) is O(N), so compute once in setup() and share with every VU.
export function setup() {
  if (THETA <= 0) return { zetan: 0, alpha: 0, eta: 0 };
  const zetan = zeta(N_ACCOUNTS, THETA);
  const zeta2 = zeta(2, THETA);
  const alpha = 1 / (1 - THETA);
  const eta =
    (1 - Math.pow(2 / N_ACCOUNTS, 1 - THETA)) / (1 - zeta2 / zetan);
  return { zetan, alpha, eta };
}

function zipf(state) {
  if (THETA <= 0) return Math.floor(Math.random() * N_ACCOUNTS);
  const u = Math.random();
  const uz = u * state.zetan;
  if (uz < 1) return 0;
  if (uz < 1 + Math.pow(0.5, THETA)) return 1;
  return Math.floor(N_ACCOUNTS * Math.pow(state.eta * u - state.eta + 1, state.alpha));
}

export default function (state) {
  const from = zipf(state);
  let to = zipf(state);
  if (to === from) to = (to + 1) % N_ACCOUNTS; // self-transfer is rejected by the script

  const res = http.post(
    `${BASE}/transfer`,
    JSON.stringify({
      from_account: from,
      to_account: to,
      amount: AMOUNT,
      currency: CURRENCY,
      memo: "k6",
      // Always send one. Without it a retry double-spends, and k6 retries.
      idempotency_key: `${__VU}-${__ITER}-${Date.now()}`,
    }),
    { headers: { "Content-Type": "application/json" } },
  );

  if (res.status === 201) okRate.add(true);
  else if (res.status === 200) { duplicates.add(1); okRate.add(true); }
  else if (res.status === 422) { insufficient.add(1); okRate.add(false); }
  else okRate.add(false);

  check(res, {
    "no server error": (r) => r.status < 500,
  });
}

export function teardown() {
  // The check that makes every number above trustworthy (ARCH_DESIGN.md §6.1).
  // Compare against the value printed by /admin/seed before the run.
  const res = http.get(`${BASE}/admin/conservation`);
  console.log(`conservation after run: ${res.body}`);
}
