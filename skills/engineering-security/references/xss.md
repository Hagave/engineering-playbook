# Cross-site scripting

CWE-79

**Load when** the backend renders HTML, serves user-supplied content to a browser, builds URLs for the client, or a frontend writes data into the DOM.

## Invariant

User data reaches HTML, JavaScript, CSS or a URL only through the encoding of that exact context.

## Sinks

- Frontend: `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `dangerouslySetInnerHTML`, `v-html`, Angular `bypassSecurityTrust*`.
- Server templates with escaping turned off: `<%- %>`, `{{{ }}}`, `|safe`, `Markup(...)`, `raw`, `html_safe`.
- HTML built by string concatenation in a handler (`res.send('<p>' + name + '</p>')`).
- `href`, `src`, `action` or `formaction` filled from input without a scheme check (`javascript:` URLs).
- JSON embedded in an HTML page (`<script>window.__STATE__ = ...</script>`) without escaping `<`.
- Error pages and 404 pages that echo the path or a query parameter.
- A response without `Content-Type`, or `text/html` on an endpoint that returns user content.

## Control

- Autoescaping templates and framework bindings (`{{ }}`, JSX text). Escaping is never disabled for data.
- When HTML is a feature (rich text), sanitize it with an allowlist sanitizer (DOMPurify, bleach, sanitize-html) on output, and keep the sanitizer updated.
- URL attributes: parse the URL and allow only `http`, `https` and `mailto` (plus relative paths).
- JSON APIs answer with `Content-Type: application/json` and `X-Content-Type-Options: nosniff`.
- CSP without `unsafe-inline` and `unsafe-eval` for scripts (see HTTP security in the skill).
- User uploads served from another origin or with `Content-Disposition: attachment` (see `files.md`).

## Negative proof

| Hostile input | Where | Expected |
|---|---|---|
| `<script>alert(1)</script>` | a field later shown in a page | rendered as text: `&lt;script&gt;` |
| `"><img src=x onerror=alert(1)>` | a value placed inside an attribute | encoded quotes; no new element |
| `javascript:alert(1)` | a URL field (profile website, link) | rejected, or rendered as inert text |
| `</script><script>alert(1)</script>` | data embedded as JSON in a page | `<` escaped as `<` |
| `<svg onload=alert(1)>` | rich-text field | sanitizer removes the handler |
| any input | JSON endpoint | `Content-Type: application/json` and `nosniff` present |

## Dynamic proof

Lab: the passive scan reports no reflected input without encoding, and no missing CSP or `nosniff` header.

## Review signal

- A sink from the list fed by a variable rather than a literal.
- An escaping bypass with a comment saying the value "is safe".
- A URL attribute set from data with no scheme check.
