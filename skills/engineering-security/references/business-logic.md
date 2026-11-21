# Business logic, races and workflow abuse

CWE-840, CWE-841, CWE-362, CWE-367, CWE-20

**Load when** the code enforces a limit, a balance, a quota, a single-use item, a price, a multi-step workflow, or any rule a user gains from breaking. The mechanics of atomic writes, locks and idempotency are in `engineering-concurrency`. This fiche is about proving the rule holds.

## Invariant

Business rules hold under concurrency, replays, out-of-order steps and boundary values. They are enforced in the domain and the database, never by the client.

## Sinks

- Check-then-act on shared state:
  - read the balance, then debit;
  - read `coupon.used`, then set it;
  - read the stock, then decrement;
  - count the invites, then insert.
- Values the server should own taken from the request: price, total, discount, currency, the current workflow step, the "approved" flag.
- Workflows whose endpoints can be called in any order (pay before confirming, confirm twice, skip verification).
- Numeric edges:
  - negative quantities, zero, very large numbers;
  - floating point for money;
  - integer overflow in a language that wraps.
- Retries and replays of state-changing requests without idempotency.

## Control

- An atomic conditional update or a database constraint does the check and the write in one step:
  - `UPDATE accounts SET balance = balance - :x WHERE id = :id AND balance >= :x`, with the affected row count checked;
  - a unique index for single-use items;
  - optimistic locking with a version column.
- Prices, totals and permissions are computed on the server from server data.
- Workflows are an explicit state machine in the domain. A transition not allowed from the current state is rejected (409).
- Money as integer minor units or a decimal type. Value objects reject out-of-range quantities.
- Idempotency keys on state-changing endpoints that clients retry.

## Negative proof

Concurrency tests run the requests truly in parallel (`Promise.all`, a thread pool) against the real database, not a mock.

| Attempt | Expected |
|---|---|
| 20 parallel redemptions of one single-use coupon | exactly 1 succeeds |
| parallel withdrawals whose sum exceeds the balance | the balance never goes below zero; failures are clean errors |
| 20 parallel purchases of the last item in stock | exactly 1 succeeds; stock is 0, not negative |
| `quantity: -1`, `0`, `2^53`, `1.5` where an integer is expected | 400 |
| `price` / `total` / `discount` in the request body | ignored; the server value is charged |
| calling step 3 of a workflow before step 2 | 409, no effect |
| the same request replayed with the same idempotency key | same response, one effect |
| the same idempotency key sent by another user | handled as a new key; the first user's response is never returned |
| the same idempotency key with a different body | 422, no effect |
| the operation fails after its write (fault injected before the commit) | the write and the key roll back together; the retry takes effect once |

## Dynamic proof

Not provable in the lab: the passive baseline sends no request bursts. The concurrent negative test above is the proof.

## Review signal

- A `find` followed by a `save` of the same row with a condition checked in application code between them.
- A monetary or permission value read from the request.
- An endpoint of a multi-step flow that does not read the current state before acting.
- An idempotency key kept in a cache or a second store for an operation that writes to the database, or not scoped to the caller.
