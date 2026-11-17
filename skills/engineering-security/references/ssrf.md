# Server-side request forgery

CWE-918

**Load when** the server makes a request to a URL or host that comes from input: webhooks, fetching an image or file by URL, link previews, import-from-URL, PDF or HTML renderers, XML/SVG processing.

## Invariant

Outbound requests reach only destinations the policy allows, checked against the resolved IP address of every hop.

## Sinks

- Any HTTP client (`fetch`, `axios`, `got`, `http.request`, `requests`, `HttpClient`) given a URL from input.
- Headless browsers and HTML-to-PDF renderers loading user HTML (it can reference internal URLs).
- XML or SVG parsers that fetch external resources (see `untrusted-parsing.md`).
- URL-shaped settings stored by tenants: webhook URLs, SSO metadata URLs, avatar URLs.

## Control

- Prefer an allowlist of hosts. Use the denylist below only when arbitrary hosts are a requirement (webhooks).
- Resolve the host and reject the request when ANY resolved address is in:
  - `0.0.0.0/8`, `10.0.0.0/8`, `100.64.0.0/10`, `127.0.0.0/8`;
  - `169.254.0.0/16` (link-local, which includes cloud metadata at 169.254.169.254);
  - `172.16.0.0/12`, `192.168.0.0/16`;
  - `::1`, `fc00::/7`, `fe80::/10`, and IPv4-mapped IPv6 forms of the ranges above.
- Connect to the address that was validated, not to the hostname again. A second resolution is how DNS rebinding passes the check. Use the HTTP client's custom lookup hook, or connect to the IP and send the `Host` header.
- Redirects off, or each hop validated again with the same rules.
- Scheme `http`/`https` only, ports allowlisted, response size and time limits.
- Defense in depth: an egress proxy or network policy that blocks the metadata endpoint and internal ranges; IMDSv2 with a hop limit of 1 on cloud VMs.

## Negative proof

| Hostile input | Expected |
|---|---|
| `http://169.254.169.254/latest/meta-data/` | rejected before any connection |
| `http://127.0.0.1:<internal port>/` and `http://localhost/` | rejected |
| `http://[::1]/` and `http://[::ffff:127.0.0.1]/` | rejected |
| `http://2130706433/`, `http://0x7f000001/`, `http://0177.0.0.1/` | rejected (alternate encodings of 127.0.0.1) |
| a hostname that resolves to `127.0.0.1` (a test DNS stub, or `localtest.me`) | rejected |
| an allowed host answering `302 Location: http://169.254.169.254/` | redirect not followed, or rejected at the second hop |
| `file:///etc/passwd`, `gopher://...`, `ftp://...` | rejected (scheme) |

Assert that no outbound connection was attempted: inject the HTTP client or the DNS resolver as a fake in the test.

## Dynamic proof

Not provable in the lab: the passive baseline submits no URL fields, and the lab runs no listener. The negative tests above are the proof.

## Review signal

- A URL from input reaching an HTTP client with no validation call in between.
- A validator that checks the hostname string (`!== 'localhost'`) instead of the resolved address.
- `maxRedirects` left at the default on a client that fetches user URLs.
