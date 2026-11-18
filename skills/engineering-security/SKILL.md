---
name: engineering-security
description: "Security rules for input validation, injection prevention, secrets management, auth, and HTTP. Use when handling user input, writing queries, managing credentials, implementing authentication or authorization, or configuring HTTP endpoints; also when code fetches URLs from input, handles files or uploads, parses untrusted data, uses cryptography, exposes GraphQL, edits CI/CD pipelines or dependencies, or calls an LLM. Holds per-vulnerability fiches in references/."
version: 2.5.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# Security

## Input validation

- Never trust external input — HTTP, events, CLI arguments, files, environment.
- Validate at every system boundary using schema validation. Tool choice depends on context:
  - **NestJS request bodies:** use `class-validator` + `class-transformer` + `ValidationPipe`; do not use Zod or Joi here.
  - **Environment variables (NestJS):** use Joi via `@nestjs/config`.
  - **Other boundaries:** use the language/framework's validation library or schema validator (class-validator, Zod, Joi, or equivalent).
- Whitelist allowed fields. Reject unknown properties.
- User-facing text inputs must have explicit length caps before reaching processing, storage, or inference layers.
- Validate format and type before processing: UUIDs, emails, enums, numeric ranges.

---

## Injection prevention

Never concatenate user input into queries, commands, or templates.

```pseudocode
# wrong — SQL injection
db.query("SELECT * FROM users WHERE id = '" + id + "'")

# correct — parameterized
db.query("SELECT * FROM users WHERE id = ?", [id])
```

- Never pass unsanitized input to shell commands, file paths, dynamic imports/module loading, or template engines.
- Log sanitization: never log raw user-provided content — it may contain injection sequences or PII. Sanitize before logging.

---

## Secrets management

- Never hardcode secrets, API keys, tokens, or credentials in source code or configuration files committed to version control.
- Secrets come from environment variables only, validated at startup.
- Never log secrets, tokens, or credentials — mask them explicitly before logging.
- Never include secrets in error messages or API responses.
- `.env` files containing real secrets must be in `.gitignore`.

### Secret scanner (hook)

The playbook hook enforces the rules above with gitleaks' rule set. A write that adds a token with a known shape (AWS, GitHub, GitLab, Stripe, Slack, private key and others) to a file git would track is denied, and so is a commit or push carrying one. A keyword-anchored match (`api_key = "..."`) is reported as a possible secret without blocking.

- On a deny, move the value to configuration read at run time (environment variable, git-ignored `.env`, secret manager). Never rename, split or encode the value to get past the scanner: the value is still in the repository.
- A false positive (test fixture, documented example) is the user's to allow, by adding the fingerprint from the deny message to `.playbook/secrets-allow`. Show the user the line; do not write that file and do not add `gitleaks:allow` markers — neither is honored when the agent writes it.
- Test fixtures should use values that cannot be real: the provider's documented example values (`AKIAIOSFODNN7EXAMPLE`) or tokens assembled at run time from fragments.

---

## Authentication and authorization

- Authenticate at the system boundary (middleware, guard, interceptor) — not inside use cases or domain logic.
- Use cases receive a verified identity object, not raw credentials or tokens.
- Enforce authorization at the use case level, not only at the route level — defense in depth.
- Token validation must verify: signature, expiry, issuer, audience.

### Token delivery

Before implementing authentication, determine who consumes the token — this decides where the token lives. Do not assume; if it is not stated, ask.

- **A browser or mobile client is on the request (server-to-client).** The backend issues and sets the tokens itself: access token and refresh token as `httpOnly` cookies (`sameSite=strict`, `secure` outside dev, `__Host-` name prefix; short-lived access, longer-lived refresh). The token is **never** returned in the response body. A token in the body is reachable by JavaScript — XSS-exfiltratable — and pushes secure storage onto the client; letting the server own the cookie removes both problems. Because the browser now sends an ambient credential automatically, CSRF protection is mandatory (see `engineering-nestjs-security`).
- **No browser/mobile client — service-to-service (server-to-server).** Return the JWT in the response body; the caller stores it and replays it as `Authorization: Bearer <token>`. No ambient cookie is set, so CSRF does not apply.

**Non-negotiable:** never return a user-authentication token in the response body when a web/mobile client is present. Backend-set `httpOnly` cookies are the default delivery for client-facing auth; body delivery is reserved for server-to-server callers.

---

## HTTP security

- Apply security headers: CSP, HSTS, X-Frame-Options, X-Content-Type-Options (Helmet or equivalent).
- Rate limit all public endpoints.
- CORS must be explicitly configured — never allow `*` in production.
- Never expose stack traces, internal error messages, or implementation details to clients.
- Use HTTPS everywhere. Never serve credentials or tokens over HTTP.
- A WAF is not a control. It reduces noise in front of the application; the fix for a vulnerability lives in the code, and a finding is not closed because a WAF rule blocks its payload.
- Behind a reverse proxy, trust `X-Forwarded-*` only from the proxy's address (Express `trust proxy` set to that hop, not `true`). The proxy and the application must agree on how a request is framed: reject requests carrying both `Content-Length` and `Transfer-Encoding`, and keep the proxy from passing ambiguous requests through (request smuggling, CWE-444).

---

## Dependencies

- Flag critical vulnerabilities from the package manager's audit command (`npm audit` or equivalent). Do not ship with known critical CVEs.
- Review `--ignore-scripts` bypass decisions — they may allow malicious install scripts.
- Keep dependency versions pinned or range-restricted to avoid unexpected upstream changes.

---

## Static analysis (when installed)

The playbook never installs or bundles a SAST engine. When `semgrep` or `opengrep` is on the PATH, the session context names it (`[playbook:sast]`), and the scan becomes part of validation for any turn that writes source:

```bash
semgrep scan --metrics=off --error --severity ERROR --config <config> <changed files>
opengrep scan --error --severity ERROR --config <local config> <changed files>
```

- **Config.** Use the project's own rules when it has them (`.semgrep.yml`, `.semgrep.yaml` or `.semgrep/`). Otherwise semgrep can use `p/default`. That downloads the registry rules, so it needs network access. The rules are licensed for use, not redistribution: never copy them into the repository. Use opengrep only with a local config.
- **Metrics stay off** (`--metrics=off`). A registry config otherwise turns metrics on.
- **Scope** is the files changed in the turn, not the whole repository: the cost stays proportional to the change.
- **Findings** are review candidates. Close each one the way `engineering-code-review` closes a security candidate: fix a confirmed finding (with its negative test, see `engineering-testing`), and report a false positive to the user. Never add a `nosemgrep` comment yourself; like the secret scanner's allow file, an exception is the user's to grant.
- **A scan that cannot run** (no network for the registry, timeout, exit code 2 or higher) does not block the work. Say that it did not run and why, and go on. The scan is an extra layer on top of review and tests, not a replacement for them.

---

## Local validation lab

The fiches' **Dynamic proof** runs against the project's own application, on this machine, inside the lab and nowhere else. The lab runs a passive baseline only: it crawls and reads responses, and sends no payload, session or request of its own. A fiche whose proof needs crafted requests says it is not provable in the lab, and its negative tests are the proof; never send those requests from outside the lab instead. The lab is `lab/run.py`, at the root of the playbook (two levels above this skill's base directory). It needs Docker with Compose 2.20 or newer.

```bash
python3 <playbook>/lab/run.py --setup                       # once: checks Docker, downloads the pinned OWASP ZAP image (about 1.2 GB, 3.8 GB on disk)
python3 <playbook>/lab/run.py --service app --port 8080     # from the project directory: scan the compose service `app`
python3 <playbook>/lab/run.py --uninstall                   # removes the scanner image
```

- **Isolation.** The lab runs an isolated copy of the project's compose stack, as its own compose project. It removes published ports, fixed names, subnets and addresses, and gives the copy fresh volumes. Every network of the copy is marked `internal`, so no service reaches this computer, the local network or the internet, whatever the project's environment points at; a service that needs the internet to start does not start in the lab, and the run ends with exit code 3. It adds the scanner, OWASP ZAP baseline (passive), on its own internal network whose only other member is the target service. It refuses a service that shares a namespace, device or container with the host (`privileged`, `pid: host`, `devices`, added capabilities and the like). The developer's running stack is not touched.
- **The target is a service, never a URL.** It is a compose service plus a port and a path. Run from the project with no `--service`, the lab finds the compose file (in the directory or a descendant; several and it lists them to pick with `--compose-file`) and scans every service built from inside the project, each in its own isolated bring-up, taking the port from `ports`/`expose`; `--service` and `--port` pick one. Nothing published (production, staging, any host not in the compose file) can be targeted, and there is no option to do so. The hook denies running scanners (`nmap`, `nuclei`, `sqlmap`, `ffuf`, ZAP and others), or scanner containers, outside `lab/run.py`.
- **Folders of this computer.** A read-only mount whose source is inside the project (committed init scripts, seed data) is kept. A read-write mount is dropped before the lab starts, so the scan writes to a fresh, empty location and never to the developer's real database or source tree; the container uses its image's own content at that path. A read-only mount outside the project, and a `configs:`/`secrets:` file outside it, are refused with the path named.
- **Nothing is kept.** The report lives in a temporary directory and is printed, then deleted. The lab's containers, network, volumes and built images are removed at the end of every run. Only the scanner image stays cached until `--uninstall`, and `--setup` states its size before downloading. Do not save the output to a file.
- **Findings.** A Medium or High alert is a review candidate. Confirm it in the code, write the negative test from its fiche first (`engineering-testing`), then fix it. The test, not the report, is the record that it stays fixed.
- **Exit codes.** `0` no Medium/High, `1` findings, `2` refused (environment or isolation, with the reason), `3` Docker or scanner error.

---

## Vulnerability fiches

The rules above are the baseline. When the code reaches one of the sinks below, read the matching fiche in `references/` (next to this file; adapters that flatten skills install it under `<skills dir>/engineering-security/references/`). Read it before writing the code, and again when reviewing it.

| Fiche | Read when the code... |
|---|---|
| `injection.md` | builds a query, shell command or template, or evaluates code, from anything external |
| `xss.md` | renders HTML, writes data into the DOM, or builds URLs for a browser |
| `redirects-and-headers.md` | redirects to a URL from the request, or writes a header from data |
| `ssrf.md` | makes an outbound request to a URL or host that comes from input |
| `files.md` | builds a file path from input, extracts archives, or accepts uploads |
| `untrusted-parsing.md` | deserializes, parses XML, merges objects from input, or runs a regex on input |
| `access-control.md` | reads or changes an object by id, exposes a privileged operation, or copies a body into an entity |
| `tokens-sessions.md` | issues or verifies JWTs, manages sessions or refresh tokens, or implements OAuth/OIDC |
| `business-logic.md` | enforces a limit, balance, quota, single-use item, price or multi-step workflow |
| `graphql.md` | exposes a GraphQL schema |
| `crypto.md` | stores passwords, generates tokens or keys, encrypts, compares secrets, or configures TLS |
| `supply-chain.md` | adds a dependency, changes a registry or base image, or edits a CI/CD pipeline |
| `llm-apps.md` | prompts a model, lets it call tools, retrieves documents into a prompt, or acts on its output |

Every fiche has the same sections: **Invariant** (what must always hold), **Sinks** (where it breaks), **Control** (the fix), **Negative proof** (hostile inputs and the expected result — these become tests, see `engineering-testing`), **Dynamic proof** (what the local lab confirms) and **Review signal** (what `engineering-code-review` looks for).

The hostile inputs in a fiche are test data for the project's own code, run locally. They are not for any system the user does not own and run locally.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
