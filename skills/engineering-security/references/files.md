# Path traversal and file upload

CWE-22, CWE-23, CWE-434, CWE-73

**Load when** a file path is built from input (download, read, write, delete, archive extraction), or the application accepts uploads.

## Invariant

A path built from input resolves inside its base directory. An uploaded file is stored under a server-generated name, outside anything that executes or renders it, and its type is decided by its content.

## Sinks

- `path.join(base, input)` or string concatenation, followed by `fs.readFile`, `fs.writeFile`, `fs.unlink`, `res.sendFile`, `res.download`, `open()`, `send_file`.
- Archive extraction (`zip`, `tar`) writing entry names as given — "zip slip".
- Upload middleware that keeps the client's filename or destination (`multer` `filename` callback, `file.originalname`).
- Type checks that trust the client: the `Content-Type` header or the extension alone.

## Control

- Prefer an id: the client sends an id, and the server maps it to a path it owns.
- When a path from input is unavoidable:
  `resolved = realpath(resolve(base, input))`, then require `resolved` to start with `realpath(base) + separator`. Reject otherwise. Validate after decoding, never before.
- Archive entries go through the same check before anything is written.
- Uploads:
  - server-generated name (UUID) and a size limit enforced while streaming;
  - type allowlist checked against the file's magic bytes;
  - stored outside the web root or in object storage;
  - served with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`, ideally from a separate origin;
  - never with execute permission.
- Images that are re-served: re-encode them, which strips metadata and polyglot payloads.

## Negative proof

| Hostile input | Expected |
|---|---|
| `../../etc/passwd` | rejected; nothing read outside the base |
| `..%2f..%2fetc%2fpasswd`, `%2e%2e/%2e%2e/etc/passwd` | rejected after decoding |
| `....//....//etc/passwd` | rejected (naive `../` stripping fails this) |
| `/etc/passwd` (absolute) | rejected (`resolve` ignores the base on an absolute path) |
| `..\..\windows\win.ini` | rejected |
| a symlink inside the base pointing outside it | rejected (realpath) |
| a zip containing an entry named `../../evil.txt` | extraction refused; no file outside the target |
| upload `avatar.png` whose bytes are HTML or SVG with script | rejected by the magic-byte check, or served as attachment |
| upload `shell.php` / `x.png.php` / `x.php%00.png` | rejected; stored name is server-generated |
| upload larger than the limit | 413, stream aborted |

## Dynamic proof

Not provable in the lab: the passive baseline sends no traversal sequences and uploads nothing. It does report a missing `X-Content-Type-Options: nosniff` on the responses it crawls. The negative tests above are the proof.

## Review signal

- `join`/`resolve` with input and no containment check after it.
- `originalname` or a client-sent path reaching the filesystem.
- A check on the extension only.
