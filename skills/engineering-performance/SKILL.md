---
name: engineering-performance
description: "Performance rules for data access, loops, and Node.js event loop. Use when writing queries, iterating over collections, handling bulk operations, or processing large datasets. Detects N+1 patterns, unbounded queries, and algorithmic complexity issues."
version: 2.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Performance

## N+1 queries

Pattern to detect: a repository or database call inside a loop.

```pseudocode
# wrong — N queries after 1 list query
for dept in departments:
  dept.employees = repo.findByDepartmentId(dept.id)

# correct — single query with join or include
departments = repo.findAllWithEmployees()

# correct — single batched query when join is not available
ids = collect(departments, id)
employees = repo.findByDepartmentIds(ids)
```

If batching is impossible (e.g., one external API call per item): cap concurrency — never fire unlimited parallel requests. Use bounded parallel execution.

---

## Unbounded queries

- Never fetch all rows without pagination (`take`/`skip` or cursor-based strategy).
- Never load an entire table into memory for processing.
- Any loop whose bound derives from user input or a DB row count must have a documented hard cap enforced in code.

Cursor-based iteration pattern:
```pseudocode
cursor = none
do:
  batch = repo.findPage(cursor, limit: 100)
  processBatch(batch)
  cursor = last_id(batch)
while batch.length == 100
```

---

## Algorithmic complexity

- Nested loops over collections: verify O(n²) is acceptable before committing. Prefer Map/Set for lookups inside loops (O(1) vs O(n)).
- Rebuilding data structures on every call that could be built once: memoize or precompute.
- Sorting the same collection multiple times: sort once, reuse.
- Searching an array in a loop: convert to Map/Set before the loop.

```pseudocode
# wrong — O(n²)
for order in orders:
  user = users.find(user.id == order.userId)

# correct — O(n)
user_map = hash_map(users, key: id)
for order in orders:
  user = user_map.get(order.userId)
```

---

## Event loop and blocking I/O

- No CPU-intensive synchronous operations in request handlers (heavy regex, large in-memory sorts, crypto without worker threads).
- No parsing or serializing large payloads in one piece without a prior size check (JSON is one example).
- No synchronous file I/O in request paths.
- Heavy or long-running computation: offload to a worker thread or background job.

---

## Memory

- Do not load large datasets into memory when streaming is possible.
- Release large object references after use — do not hold them in closures across request boundaries.
- Framework services are often singletons (NestJS default) — do not accumulate mutable state in instance variables across requests (rule owned by `engineering-design-patterns`, Singleton section; here the concern is memory growth).
- In-memory caches in singletons do not invalidate across replicas — delegate to shared cache (Redis).

---

## Async parallelism

These rules cover parallel async execution as a throughput concern. For distributed coordination (locks, idempotency, race conditions across replicas), see `engineering-concurrency`.

- Use bounded parallel execution for bulk operations. Never fire N unlimited parallel requests where N comes from user input or DB rows.
- Use the language's parallel/batch execution API for a fixed, small set of independent operations (`Promise.all` in JS/TS).
- Use the "settle all" variant when partial failure is acceptable and all results must be collected (`Promise.allSettled` in JS/TS).
- Use sequential iteration with `await` only when operations are dependent or back-pressure matters.
- Multiple related writes are not atomic with concurrent async writes — use transactions instead.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
