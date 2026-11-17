---
name: engineering-concurrency
description: "Concurrency rules for shared state, distributed operations, and idempotency. Use when implementing read-modify-write operations, distributed locks, idempotency keys, rate limiting, or background jobs across multiple replicas."
version: 2.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
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

POST endpoints that mutate state must be idempotent when called with the same `Idempotency-Key`.

Protocol:
1. Client sends `Idempotency-Key: <uuid>` header.
2. Before executing the use case: check if the key exists in Redis.
3. If it exists: return the cached response immediately — do not re-execute.
4. If it does not exist: execute the use case, store `key → response` in Redis with a TTL (24h standard), return response.
5. On conflict: return `409 Conflict` with the existing resource ID.

Storage pattern — always use atomic `SET NX` to claim the key before executing. A `GET` + conditional `SET` creates a race window: two concurrent requests with the same key both see a cache miss and both execute the use case.

```pseudocode
key = "idempotency:" + idempotency_key

# Atomic claim — only the first request proceeds; concurrent duplicates are blocked
claimed = redis.set(key, "processing", NX, EX, 86400)
if not claimed:
  cached = redis.get(key)
  if cached exists and cached != "processing":
    return parse_response(cached)
  # 'processing' means a concurrent request is in flight with the same key
  # conflict response carries the idempotency key back to the client
  throw duplicate_request("Request already in progress", idempotency_key)

result = operation.execute(input)
# Overwrite 'processing' placeholder with the actual response
redis.set(key, serialize(result), EX, 86400)
return result
```

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
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
