# Tokens, sessions and OAuth

CWE-347, CWE-287, CWE-384, CWE-613, CWE-352, CWE-601

**Load when** the code issues or verifies JWTs, manages sessions or refresh tokens, or implements an OAuth 2.0 / OpenID Connect client or callback. Cookie delivery and CSRF rules are in the skill itself and in `engineering-nestjs-security`.

## Invariant

A token is accepted only when the server verified it with an algorithm and a key the server chose. A session ends when the server says so. An OAuth response is bound to the request that started it.

## Sinks

- JWT verification that:
  - reads the algorithm from the token header;
  - accepts `none`;
  - uses one key for both symmetric and asymmetric algorithms;
  - decodes without verifying (`jwt.decode` used for authorization).
- Missing `exp`, `iss` or `aud` checks. Long-lived access tokens.
- Refresh tokens that never rotate, or are not invalidated when reused.
- Session ids kept across login (fixation). Logout that only deletes the cookie on the client.
- OAuth:
  - no `state` parameter, or a `state` not bound to the user's session;
  - no PKCE;
  - `redirect_uri` matched by prefix or wildcard;
  - the implicit flow;
  - an `id_token` accepted without checking `iss`, `aud`, `nonce` and `exp`.

## Control

- Pin the algorithm per key on verify (`algorithms: ['RS256']`). Keys come from configuration or a JWKS with a pinned issuer.
- Verify signature, `exp`, `nbf`, `iss` and `aud` on every request. Access tokens are short-lived (minutes).
- Refresh tokens rotate on every use and are stored hashed. Reusing a rotated token revokes the whole family.
- Revocation: a session version or `jti` checked on sensitive operations. Logout invalidates server-side.
- Regenerate the session id on login and on privilege change.
- OAuth/OIDC:
  - authorization code flow with PKCE (`S256`);
  - `state` and `nonce` generated per request and bound to the session;
  - exact `redirect_uri` registration;
  - full `id_token` validation;
  - tokens never logged and never put in URLs.

## Negative proof

| Attempt | Expected |
|---|---|
| a token with header `{"alg":"none"}` and an empty signature | 401 |
| a token signed with HS256, using the server's RSA public key as the secret | 401 |
| a valid token with its payload edited (`"role":"admin"`) | 401 |
| an expired token; a token with a wrong `aud` or a wrong `iss` | 401 |
| a refresh token used twice | second use 401, and the newer token of the family revoked too |
| a session id from before login, presented after login | not the authenticated session |
| a request after logout with the old session or token | 401 |
| an OAuth callback with no `state`, or with another session's `state` | rejected, no login |
| an authorization request with `redirect_uri` `https://app.example.com.evil.example/cb` | rejected by the provider's registration |

## Dynamic proof

Partly in the lab: the passive baseline reports session cookies without `HttpOnly`, `Secure` or `SameSite`, and forms without an anti-CSRF token. Replaying a token after logout or with a modified payload is not provable there. The negative tests above are the proof.

## Review signal

- `decode` where `verify` belongs.
- `verify` without an `algorithms` option.
- A token or session lifetime measured in days for access.
- An OAuth callback that does not read `state`.
