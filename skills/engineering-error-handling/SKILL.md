---
name: engineering-error-handling
description: "Error handling rules: exception hierarchy, retry patterns, circuit breakers, timeouts, and graceful degradation. Use when implementing exception handling, calling external services, writing retry logic, or defining fallback behavior."
version: 3.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# Error Handling

## Exception hierarchy

All domain exceptions extend a single base class. Never throw raw `Error` from domain or application layers.

Standard HTTP classifications — apply at the infrastructure/presentation boundary, not inside business logic:

| Type | HTTP | Use for |
|---|---|---|
| Validation error | 400 | Invalid input format or structure |
| Unauthorized | 401 | Missing or invalid credentials |
| Forbidden | 403 | Authenticated but not authorized for this action |
| Not found | 404 | Requested entity does not exist |
| Conflict / Duplicate | 409 | Idempotency conflict, duplicate resource |
| Business rule violation | 422 | Valid input, but operation violates a business invariant |
| External service failure | 502 | Upstream dependency failed |

Never throw HTTP exception types from use cases or domain code.

---

## Error observability — non-negotiable

Every exception that reaches a catch block **must be logged before anything else**. The log entry is the primary artifact for diagnosing production failures. Two rules are absolute:

**Rule 1 — Log always contains: who threw, what happened, full context.**

```pseudocode
logger.error("PaymentService.charge failed", {
  service: "PaymentService",        # who threw
  method: "charge",                 # where in the service
  error: original_error_message,    # what happened
  stack: full_stack_trace,          # full stack trace
  userId,                           # relevant entity IDs
  orderId,
  correlationId: request_correlation_id,  # request traceability
})
```

Minimum required fields in every error log:
- `service` — class or module name that caught the exception
- `method` — method or function where it was caught
- `error` — original `err.message`, never a paraphrase
- `stack` — full stack trace
- At least one entity ID (userId, orderId, correlationId, or requestId)

**Rule 2 — Client response never exposes internals, and carries only a standardized envelope.**

Errors and exceptions use the same standardized structure as successful responses — one predictable shape the client can always parse: `statusCode` + `error { code, message }`. The `code` is a stable, machine-readable string the client discriminates on (`NOT_FOUND`, `VALIDATION_ERROR`), decoupled from the internal exception class name. A 5xx always collapses to a fixed generic envelope.

```pseudocode
# exception filter — the only place that maps exceptions to HTTP responses.
# 5xx: sanitized, generic. The full detail (stack, service, method) is in the logs only.
{ statusCode: 500, error: { code: "INTERNAL_ERROR", message: "An unexpected error occurred" } }
```

What the client must never receive:
- Stack traces
- Internal file paths
- Raw ORM/DB error messages (column names, constraint names)
- Service or class names
- Original exception messages from infrastructure layers (5xx)
- **Debug/correlation metadata in the body** — `traceId`/`correlationId`, request `path`, `timestamp`. These are not part of the response contract.

The correlation ID is the bridge between the client error and the log, but it travels in the **`x-trace-id` response header**, never in the body — the client can quote it to support without it polluting the payload. It must always appear in the header and in every server log line. For the NestJS envelope and filter implementation, see `engineering-nestjs-interceptors`.

For logger setup and structured log field conventions, see `engineering-nestjs-logger`.

---

## Error propagation

**Infrastructure boundary (repository / adapter):** catch infrastructure exceptions (ORM errors, HTTP client errors, Redis errors) here and rethrow as domain exceptions. Never let persistence or network errors leak into application or domain layers.

**Use cases:** propagate domain exceptions up the stack — do not wrap use case bodies in `try/catch`. A generic `try/catch` in a use case is an anti-pattern: it swallows typed domain exceptions and loses call-site context. The exception filter at the HTTP boundary handles all uncaught exceptions.

`try/catch` in a use case is only valid for **compensation logic** — rolling back an external side effect when a subsequent step fails:

```pseudocode
# correct — compensation only
external_payment_gateway.charge(amount)
try:
  order_repository.save(order)
catch error:
  external_payment_gateway.refund(amount)  # compensate the already-sent charge
  raise error
```

Additional rules:
- Catch specific exception types before catching generic `Error`.
- Never swallow exceptions silently.

---

## Retry patterns

- Retry only on transient errors: 429, 500, 502, 503, 504.
- Never retry on 400, 401, 403, 404 — these will not self-resolve.
- Use exponential backoff with jitter.
- Always define a maximum attempt count.
- Never wrap a retry helper in another retry loop.
- Log each retry attempt with attempt number, delay, and reason.

---

## Circuit breaker

Apply to external services that can degrade:

1. **Closed** — normal operation.
2. **Open** — after N consecutive failures in a time window, fail fast without calling the service.
3. **Half-open** — after cooldown, allow one probe request.

Use an established library (Node example: `opossum`, or the language/framework equivalent). Do not hand-roll a circuit breaker.

---

## Timeouts

Every external call must have an explicit timeout. Never rely on the network to eventually respond. Hanging indefinitely is a failure mode, not a waiting state.

---

## Graceful degradation

For every non-critical dependency, define fallback behavior before writing the integration.

| Dependency unavailable | Acceptable fallback | Never |
|---|---|---|
| Cache layer (Redis) | Serve from primary source (slower, correct) | Return stale data silently |
| External enrichment API | Return response without the enriched field, log warning | Return empty/null silently |
| Non-critical downstream service | Return 503 with `Retry-After` header | Hang or return partial data without flagging it |

Document the degraded behavior with a comment at the integration point.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
