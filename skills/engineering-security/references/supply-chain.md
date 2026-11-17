# Supply chain and CI/CD

CWE-829, CWE-1357, CWE-494, CWE-1395

**Load when** the change adds or upgrades a dependency, changes the registry configuration, edits a Dockerfile base image, or touches a CI/CD pipeline (GitHub Actions, GitLab CI, any runner).

## Invariant

What is built and run is exactly what was reviewed: pinned, from the intended source, verified. The pipeline cannot be steered by untrusted input, and it does not hand secrets to it.

## Sinks

- Dependencies:
  - `npm install` / `pip install` without a lockfile in CI;
  - lockfiles not committed;
  - internal package names that are unscoped or unregistered publicly (dependency confusion);
  - registries mixed in `.npmrc` / `pip.conf` without a per-scope mapping.
- New dependencies with few maintainers, very recent publication, names one typo away from popular ones, or `postinstall` scripts.
- `curl ... | sh` installers, and downloads without checksum or signature verification.
- `FROM image:latest` or an unpinned tag.
- GitHub Actions:
  - `uses: owner/action@v1` (a mutable tag);
  - `pull_request_target` or `workflow_run` jobs that check out and run the PR's code;
  - `${{ github.event.* }}` (titles, branch names, bodies) interpolated directly into `run:`;
  - default or `write-all` `permissions`;
  - secrets printed or passed to untrusted steps;
  - self-hosted runners on public repositories.

## Control

- Lockfile committed. CI installs with `npm ci`, `pnpm install --frozen-lockfile`, `pip install --require-hashes` or the equivalent.
- Internal packages live under an owned scope, mapped to the internal registry.
- A new dependency is reviewed before it is added: maintainers, age, downloads, repository, install scripts. The skill's audit rule still applies.
- Actions pinned to a full commit SHA, with the tag in a comment. `permissions: contents: read` at the top; widen per job.
- Untrusted event data goes through `env:` and is quoted in the script, never interpolated into it.
- No secrets in jobs that run untrusted code.
- Base images pinned by digest. Downloaded tools verified by checksum or signature.
- Optional linters: `actionlint` and `zizmor` for workflows, the package manager's audit for dependencies.

## Negative proof

| Check | Expected |
|---|---|
| CI with a `package.json` change and no lockfile update | the install step fails |
| a workflow line `uses: <action>@<tag>` | the workflow lint fails (SHA pin required) |
| a PR titled `x"; echo injected; #` on a workflow that prints the title | the title is printed literally; nothing else runs |
| an internal package name looked up on the public registry | not resolvable there, or owned by the organization |
| the Dockerfile `FROM` line | carries a `@sha256:` digest |

## Dynamic proof

Not applicable in the lab. These are pipeline and repository checks, proven in CI.

## Review signal

- A new entry in `dependencies`.
- A workflow file diff containing `${{ github.event`, `pull_request_target`, `@v`, or `permissions:`.
- A Dockerfile `FROM` with a tag and no digest.
