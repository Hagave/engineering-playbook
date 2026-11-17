# Access control — object level, function level, mass assignment

CWE-639, CWE-862, CWE-863, CWE-285, CWE-915

**Load when** an endpoint, resolver, consumer or job reads or changes an object by id, exposes a privileged operation, or copies request data into an entity.

## Invariant

- Every read or write of an object is scoped by the caller's identity or tenant where the data is fetched.
- Every privileged operation checks the caller's role in the use case, not only at the route.
- The fields a caller can write are an explicit allowlist.

## Sinks

- Fetch by an id from the request with no owner or tenant in the query: `findById(req.params.id)`, `update(id, body)`, `delete(id)`.
- List and search endpoints whose filter does not include the tenant.
- Admin operations protected only by hiding them in the UI, or by a route prefix.
- Request bodies spread into entities or ORM calls: `{ ...req.body }`, `Object.assign(entity, body)`, `update({ where, data: body })`.
- Identity taken from the request instead of the verified token: `userId`/`tenantId` in the body, query or a custom header.
- Event consumers and jobs that act on ids from the message without checking the message's tenant.

## Control

- Repository methods take the owner or tenant as a required argument (`findByIdForOwner(id, ownerId)`). The id alone is not enough to load a user-owned object.
- Return 404 for an object that exists but belongs to someone else. A 403 confirms that it exists.
- Deny by default: a guard on every route, an explicit `@Public()`-style marker for the exceptions, and a role check in the use case for privileged actions (see the skill: authorization at the use case level).
- Input DTOs list the writable fields; unknown properties are rejected (`whitelist` + `forbidNonWhitelisted`, or the schema equivalent). Privileged fields (`role`, `isVerified`, `tenantId`, `ownerId`, `balance`) never appear in a user-facing DTO.
- Identity comes from the verified token only.

## Negative proof

Two users, A and B, in different tenants, plus one admin. Each row is an integration test through the HTTP or resolver layer.

| Attempt | Expected |
|---|---|
| A reads B's object by id | 404 |
| A updates or deletes B's object by id | 404, and B's object unchanged in the database |
| A lists objects with B's `tenantId` in the query or a header | only A's objects |
| A (non-admin) calls each admin operation | 403, no effect |
| A sends `{"role": "admin"}` in a profile update | 400 (unknown field), role unchanged |
| A sends `{"tenantId": "<B's tenant>"}` or `{"ownerId": "<B>"}` on create | 400, or the server-assigned value |
| A walks sequential ids around their own | only A's objects return; the others are 404 |
| a message for tenant B processed with A's context | rejected |

## Dynamic proof

Not provable in the lab: the scan is a passive baseline that crawls as one anonymous client, with no second session to replay. The negative tests above are the proof.

## Review signal

- An id from the request reaching a repository call that has no owner or tenant parameter.
- A spread of the request body into persistence.
- A new privileged endpoint with no role check in its use case.
