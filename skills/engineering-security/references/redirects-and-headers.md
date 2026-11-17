# Open redirect and header injection

CWE-601, CWE-113

**Load when** the code redirects to a URL that comes from the request (`next`, `returnUrl`, `redirect_uri`), or writes a response header from data.

## Invariant

A redirect leaves only for a destination on an allowlist. A header value never carries raw input.

## Sinks

- `res.redirect(req.query.next)`, `Location` headers, `<meta http-equiv="refresh">`, client-side `window.location = param`.
- OAuth and SSO callbacks that forward to a `redirect_uri` or `state`-carried URL.
- `res.setHeader(name, value)` with input in the value, `Content-Disposition` filenames, cookies whose value or attributes come from input.

## Control

- Redirect only to relative paths. Accept a path when it starts with exactly one `/`, followed by neither `/` nor `\`. Otherwise fall back to a fixed default.
- When absolute URLs are needed, parse the URL and compare the scheme and the host against an exact allowlist. Never use a prefix or a `contains` check: `https://app.example.com.evil.example` passes those.
- Reject CR (`\r`) and LF (`\n`) in any header value. Node rejects them in `setHeader`, but proxies, other runtimes and hand-built raw responses may not.
- `Content-Disposition` filenames are encoded with RFC 5987 (`filename*=UTF-8''...`), with quotes and control characters removed.

## Negative proof

| Hostile input | Where | Expected |
|---|---|---|
| `https://evil.example` | `next` parameter | redirect to the default path |
| `//evil.example` | `next` parameter | redirect to the default path |
| `/\evil.example` | `next` parameter | redirect to the default path |
| `https:evil.example` | `next` parameter | redirect to the default path |
| `%2F%2Fevil.example` | `next` parameter, decoded once | redirect to the default path |
| `javascript:alert(1)` | `next` parameter | redirect to the default path |
| `https://app.example.com.evil.example` | absolute-URL allowlist | rejected |
| `a\r\nSet-Cookie: s=1` | value written to a header | rejected; no second header |
| `report"; filename="x.html` | download filename | encoded; one `filename*` parameter |

## Dynamic proof

Lab: the `Location` header of every redirect stays on the application's origin.

## Review signal

- A request parameter reaching `redirect`/`Location` with no comparison against a fixed list.
- `startsWith('http')`, `includes(domain)` or a regex without anchors used as a destination check.
