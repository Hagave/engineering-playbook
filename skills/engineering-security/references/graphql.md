# GraphQL

CWE-770, CWE-639, CWE-285, CWE-200

**Load when** the project exposes a GraphQL schema. Object-level authorization from `access-control.md` applies to every resolver as well.

## Invariant

Every resolver authorizes the object and the field it returns. The cost of any query is bounded before it executes.

## Sinks

- Resolvers that load by id without owner or tenant checks, including the `node(id:)` and `nodes(ids:)` global lookups.
- Authorization done once at the gateway or on the root field only, while nested fields reach other users' data.
- Unbounded depth (`user { friends { friends { ... } } }`), unbounded list sizes (`first: 1000000`), aliases repeating an expensive field, and batched operations in one request.
- Login or OTP mutations repeated through aliases or batching, which turns per-request rate limits into per-query.
- Introspection and field suggestions enabled in production for a private API. Error messages carrying stack traces or SQL.

## Control

- Authorization in the resolver (or the domain it calls) for every type that holds user data. The gateway check is an addition, not the control.
- Limits validated before execution:
  - query depth and cost/complexity;
  - a maximum `first`/`limit` with a default;
  - a cap on aliases and on batched operations per request.
- Rate limits counted per operation and per alias for sensitive mutations, not per HTTP request.
- DataLoader or batching for nested lists, which also limits the cost of N+1 (see `engineering-performance`).
- Introspection and suggestions off in production when the schema is not public. Persisted queries for first-party clients.
- Errors masked in production.

## Negative proof

| Attempt | Expected |
|---|---|
| a query nested 20 levels deep | rejected before execution |
| `first: 100000` | capped to the maximum, or rejected |
| one request with 1000 aliases of `login(...)` | rejected, or limited per alias |
| `node(id: "<another user's object id>")` | `null` or a forbidden error, no data |
| a nested field reaching another tenant's data through a shared parent | `null` or forbidden |
| `{ __schema { types { name } } }` in production | rejected |
| a query with a typo in the production environment | no "did you mean" suggestion |

## Dynamic proof

Not provable in the lab: the passive baseline sends no queries of its own. The negative tests above are the proof.

## Review signal

- A resolver taking an `id` argument with no ownership check.
- A server setup without depth or complexity validation rules.
- A connection field with no maximum page size.
