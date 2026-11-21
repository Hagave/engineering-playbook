---
name: engineering-concurrency
description: "Concurrency rules for shared state, distributed operations, and idempotency. Use when implementing read-modify-write operations, distributed locks, idempotency keys, rate limiting, or background jobs across multiple replicas."
version: 3.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Concurrency

Load this skill when implementing: shared state mutation, distributed operations, idempotency, background jobs, or any pattern that requires coordination across requests or replicas.

---

## Read-modify-write race conditions

Every read-modify-write sequence that is not atomic is a race condition under concurrent requests.

```pseudocode
# wrong — two concurrent requests read the same value before either writes
record = repo.findById(id)
record.balance -= amount
repo.save(record)

# correct option 1 — atomic update (preferred when possible)
updated = db.account.update(id, balance: balance - amount)

# correct option 2 — optimistic locking with version field
updated = db.account.update(id, version: current_version, balance: balance - amount, version: version + 1)
# if updated count == 0: another request won the race — throw and retry at use case level
```

Use optimistic locking when contention is low and retrying is cheap.
Use pessimistic locking (SELECT FOR UPDATE) when contention is high or retrying is costly.

---

## Distributed locks

Use when a critical section must run exclusively across multiple replicas.

Pattern: `SET NX PX` — atomic acquire. Never use `GET` + conditional `SET` (TOCTOU window).

```pseudocode
lock_key = "lock:" + resource_id
lock_token = random_id()  # platform-native random ID

acquired = redis.set(lock_key, lock_token, NX, PX, 5000)
if not acquired:
  throw domain_conflict("Resource is locked by another operation")

try:
  # critical section
finally:
  # Lua script for atomic check-and-delete (prevents releasing another holder's lock)
  redis.eval(
    'if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("del", KEYS[1]) else return 0 end',
    1, lock_key, lock_token
  )
```

Lock TTL must be set conservatively — longer than the critical section, shorter than the acceptable wait time.
Never delete a lock without the Lua script — a plain `DEL` can release another holder's lock after TTL expiry and re-acquire.

---

## Idempotency

A state-changing request that a client may retry (a POST from an app, a webhook, a payment) takes effect once, however many times it arrives. The client sends an `Idempotency-Key` header with a value it generates once per logical operation (a UUID).

Rules:
1. **The key belongs to the caller.** The record is unique on (caller identity, key). The same key sent by another caller is a different key and never returns the first caller's response.
2. **The key is bound to the request.** Store a fingerprint of the request: a hash of the method, the route and the canonical body. The same key with another fingerprint is refused with `422`, with no effect.
3. **The record lives where the effect lives.** When the operation writes to a database, the idempotency record is a row in that database, written in the same transaction as the effect, under a unique index on (caller, key). The response is stored in that row before the commit. The effect and the record commit together or roll back together, so a retry after a failure runs cleanly once. Never keep it in a second store (a cache, Redis) for such an operation: a crash between the commit and the second write leaves an effect with no record, so the retry runs it again, or leaves a record that never finishes.
4. **A concurrent duplicate never runs twice.** The second request's insert meets the unique index. Depending on the database, it waits for the first transaction and then replays the stored response, or it fails at once and gets `409` ("in progress", with the key).
5. **A replay returns the stored status and body**, without executing again.
6. **Retention is a documented window** (24 h is common), purged by a job. A record is never removed while its client may still retry the operation.

```pseudocode
fingerprint = hash(method, route, canonical(body))

transaction:
  # unique index on (caller, key); the claim is never visible outside this transaction
  claimed = idempotency.insert_if_absent(caller, key, fingerprint)
  if not claimed:
    record = idempotency.find(caller, key)
    if record.fingerprint != fingerprint:
      throw unprocessable("Idempotency-Key reused with another request")   # 422
    if record.response exists:
      return record.response
    throw duplicate_request("Request already in progress", key)            # 409
  result = operation.execute(input)          # every write of the effect, same transaction
  idempotency.store_response(caller, key, serialize(result))
commit
return result
```

**An operation with no database effect**, such as a call to an external API, has nothing to share a transaction with:
- **Prefer the provider's own idempotency:** pass the key on, since most payment APIs accept one.
- **Otherwise, claim the key in a shared store** with an atomic set-if-absent that carries the fingerprint, never a read followed by a write. Release the claim when the call fails, and store the response after it succeeds. A crash after the external call and before the store still repeats the call on retry. Write that trade-off down where the code makes it.

Status codes follow the IETF `Idempotency-Key` header draft: `422` for a key reused with another request, `409` for a duplicate still in progress.

---

## Business-level rate limiting (Redis)

**Boundary rule — two mechanisms, two layers, never stacked without justification:**

| Mechanism | Layer | When to use |
|---|---|---|
| HTTP framework rate limiter (NestJS example: `ThrottlerBehindProxyGuard`, see `engineering-nestjs-security`) | HTTP boundary | Rate limit by IP or connection. Protects endpoints from abuse regardless of identity. |
| Redis Lua script (below) | Application logic | Rate limit by business entity — user ID, resource ID, operation. Enforced inside a use case. |

Do not stack both on the same endpoint without explicit justification. If the limit is "N requests per IP", use the guard. If the limit is "user X may perform operation Y at most N times per hour", use Redis. If both apply, document why in a comment at the call site.

For per-user or per-resource rate limits enforced in application logic, use the Lua script — it is atomic and eliminates the TTL race window:

```pseudocode
key = "rate:" + operation + ":" + user_id

# Atomic: INCR and EXPIRE execute as a single Redis command.
# A plain INCR + EXPIRE sequence is not atomic — if the process crashes between them,
# the key persists without a TTL and accumulates indefinitely.
count = redis.eval(
  'local n = redis.call("INCR", KEYS[1])
   if n == 1 then redis.call("EXPIRE", KEYS[1], ARGV[1]) end
   return n',
  1, key, 3600
)

if count > limit:
  throw business_rule("Rate limit exceeded. Try again later.")
```

Never use `GET` + conditional `SET` for rate limiting — `INCR` is atomic at the counter level. For full atomicity (counter + TTL), always use the Lua script above.

---

## Background jobs

- Long-running operations must not block HTTP request handlers — use async job patterns (queues, workers).
- Jobs must be idempotent — they may be re-enqueued or retried after failure.
- Design the job so that running it twice on the same input produces the same result.
- Emit structured log events at job start, completion, and failure — include entity IDs, item counts, and duration.
- Never fire-and-forget a job without a mechanism to detect and handle failure.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
