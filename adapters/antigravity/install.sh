#!/usr/bin/env bash
# Engineering Playbook — Google Antigravity installer
#
# Usage: ./adapters/antigravity/install.sh [--git-hooks] [--ci] /path/to/target-workspace
#
# Installs the playbook into an Antigravity workspace:
#   <target>/AGENTS.md              — always-loaded core (idempotent marker block)
#   <target>/.agents/skills/        — all skills, YAML frontmatter stripped
#   <target>/.agents/workflows/     — /engineering-playbook and /playbook-* slash commands
#
# With --git-hooks, additionally installs mechanical enforcement that does not
# depend on the IDE (Antigravity has no hook system):
#   <target>/.git/hooks/commit-msg  — blocks AI co-author attribution
#   <target>/.git/hooks/pre-commit  — validation gate: tsc --noEmit + lint
#
# Git hooks run on the agent's machine, so `git commit --no-verify` skips them.
# With --ci, additionally writes the check nothing local can skip:
#   <target>/.github/workflows/engineering-playbook.yml — on every pull
#   request, fails on AI attribution in its commits or body and on a broken
#   typecheck or lint. Made a required check in branch protection, no one
#   gets past it.
#
# Skills remain the single source of truth in this repository — re-run the
# installer after updating them. The marker block in AGENTS.md is replaced,
# never duplicated; content outside the markers is preserved. Existing git
# hooks not created by this installer are never overwritten.

set -euo pipefail

ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${ADAPTER_DIR}/../.." && pwd)"

GIT_HOOKS=0
CI=0
TARGET=""
for arg in "$@"; do
  case "${arg}" in
    --git-hooks) GIT_HOOKS=1 ;;
    --ci) CI=1 ;;
    -*) echo "unknown option: ${arg}" >&2; exit 1 ;;
    *) TARGET="${arg}" ;;
  esac
done

if [[ -z "${TARGET}" ]]; then
  echo "usage: $0 [--git-hooks] [--ci] /path/to/target-workspace" >&2
  exit 1
fi
if [[ ! -d "${TARGET}" ]]; then
  echo "error: target workspace does not exist: ${TARGET}" >&2
  exit 1
fi
if [[ "${GIT_HOOKS}" -eq 1 && ! -d "${TARGET}/.git" ]]; then
  echo "error: --git-hooks requires the target to be a git repository" >&2
  exit 1
fi

SKILLS_SRC="${REPO_ROOT}/skills"
SKILLS_DST="${TARGET}/.agents/skills"
WORKFLOWS_DST="${TARGET}/.agents/workflows"
AGENTS_MD="${TARGET}/AGENTS.md"
TEMPLATE="${ADAPTER_DIR}/AGENTS.template.md"
MARK_START='<!-- engineering-playbook:start -->'
MARK_END='<!-- engineering-playbook:end -->'
HOOK_MARK='# engineering-playbook hook'

mkdir -p "${SKILLS_DST}" "${WORKFLOWS_DST}"

# 0. Upgrade from 3.x: commands and skills 4.0 dropped or folded into
#    /playbook-config. Copying the current files over an old install would
#    leave these behind, still in the / menu and still read as skills.
LEGACY_WORKFLOWS=(engineering-playbook-on engineering-playbook-off engineering-playbook-status)
LEGACY_SKILLS=(engineering-playbook-on engineering-playbook-off engineering-playbook-status
               engineering-prose engineering-watch engineering-agents)
for name in "${LEGACY_WORKFLOWS[@]}"; do
  rm -f "${WORKFLOWS_DST}/${name}.md"
done
for name in "${LEGACY_SKILLS[@]}"; do
  rm -f "${SKILLS_DST}/${name}.md"
done

# 1. Copy skills, stripping the Claude Code YAML frontmatter (Antigravity
#    skills are plain markdown; the frontmatter fields are meaningless there).
count=0
for skill_dir in "${SKILLS_SRC}"/*/; do
  name="$(basename "${skill_dir}")"
  src="${skill_dir}SKILL.md"
  [[ -f "${src}" ]] || continue
  awk '
    NR == 1 && $0 == "---" { in_fm = 1; next }
    in_fm && $0 == "---"   { in_fm = 0; skipping_blank = 1; next }
    in_fm                  { next }
    skipping_blank && $0 == "" { next }
    { skipping_blank = 0; print }
  ' "${src}" > "${SKILLS_DST}/${name}.md"
  # Reference files a skill reads on demand (engineering-security's fiches).
  # The flattened skill names them as <skills dir>/<name>/references/.
  if [[ -d "${skill_dir}references" ]]; then
    rm -rf "${SKILLS_DST:?}/${name}/references"
    mkdir -p "${SKILLS_DST}/${name}"
    cp -R "${skill_dir}references" "${SKILLS_DST}/${name}/references"
  fi
  count=$((count + 1))
done
echo "skills installed: ${count} -> ${SKILLS_DST}"

# 2. Install workflows (slash commands). Antigravity derives the command name
#    from the file basename: engineering-playbook.md -> /engineering-playbook.
wf_count=0
commands=()
for wf in "${ADAPTER_DIR}/workflows/"*.md; do
  [[ -f "${wf}" ]] || continue
  base="$(basename "${wf}")"
  cp "${wf}" "${WORKFLOWS_DST}/${base}"
  commands+=("/${base%.md}")
  wf_count=$((wf_count + 1))
done
echo "workflows installed: ${wf_count} -> ${WORKFLOWS_DST}"
echo "slash commands registered: ${commands[*]}"

# 3. Write the AGENTS.md marker block (replace if present, append otherwise).
#    The template names the clone (@@PLAYBOOK@@) so the agent can run
#    scripts/playbook_config.py from the workspace: settings are read there.
#    Under the home directory it is written as `$HOME/...`: AGENTS.md is
#    usually committed, and an absolute path would put the user name in the
#    team's repository and break for every teammate.
shopt -u patsub_replacement 2>/dev/null || true  # bash 5.2: `&` in a path stays literal
playbook_ref="${REPO_ROOT}"
if [[ -n "${HOME:-}" && "${REPO_ROOT}" == "${HOME}"/* ]]; then
  playbook_ref='$HOME'"${REPO_ROOT#"${HOME}"}"
fi
rendered_template="$(mktemp)"
trap 'rm -f "${rendered_template}"' EXIT
while IFS= read -r line || [[ -n "${line}" ]]; do
  printf '%s\n' "${line//@@PLAYBOOK@@/${playbook_ref}}"
done < "${TEMPLATE}" > "${rendered_template}"
TEMPLATE="${rendered_template}"
if [[ -f "${AGENTS_MD}" ]] && grep -qF "${MARK_START}" "${AGENTS_MD}"; then
  tmp="$(mktemp)"
  awk -v start="${MARK_START}" -v end="${MARK_END}" -v tpl="${TEMPLATE}" '
    $0 == start { skip = 1; while ((getline line < tpl) > 0) print line; close(tpl); next }
    $0 == end   { skip = 0; next }
    !skip       { print }
  ' "${AGENTS_MD}" > "${tmp}"
  mv "${tmp}" "${AGENTS_MD}"
  echo "AGENTS.md: marker block replaced"
else
  if [[ -f "${AGENTS_MD}" ]]; then
    printf '\n' >> "${AGENTS_MD}"
  fi
  cat "${TEMPLATE}" >> "${AGENTS_MD}"
  echo "AGENTS.md: marker block appended"
fi

# 4. Optional git hooks — the only mechanical enforcement available in
#    Antigravity. Never overwrite a hook this installer did not create.
install_hook() {
  local name="$1" body="$2"
  local path="${TARGET}/.git/hooks/${name}"
  if [[ -f "${path}" ]] && ! grep -qF "${HOOK_MARK}" "${path}"; then
    echo "git hook ${name}: existing hook found, NOT overwritten (add the playbook logic manually)" >&2
    return 0
  fi
  printf '%s\n' "${body}" > "${path}"
  chmod +x "${path}"
  echo "git hook installed: ${name}"
}

if [[ "${GIT_HOOKS}" -eq 1 ]]; then
  # The rule is the playbook's own (scripts/check_commit_message.py); the hook
  # calls it, so a fix there reaches every harness without a reinstall. Git
  # may run the hook with a PATH whose python3 is older than 3.10 (the macOS
  # Command Line Tools ship 3.9), or after the clone moved: then the same rule,
  # as the POSIX ERE playbook_core keeps for `git log --grep`, rendered here,
  # still blocks. A hook that let the commit through there was off for every
  # default macOS setup.
  checker="$(printf '%q' "${REPO_ROOT}/scripts/check_commit_message.py")"
  fallback="$(python3 -c 'import shlex, sys; sys.path.insert(0, sys.argv[1]); import playbook_core; print(" ".join("-e " + shlex.quote(p) for p in playbook_core._AI_ATTRIBUTION_GREP))' \
    "${REPO_ROOT}/hooks")" || {
    echo "error: --git-hooks needs python3 3.10+ to render the attribution rule" >&2
    exit 1
  }
  install_hook 'commit-msg' "$(cat <<HOOK
#!/usr/bin/env bash
# engineering-playbook hook — AI attribution guard
# Blocks AI attribution in commit messages (team policy), through the
# playbook's checker when python3 3.10+ and the clone are reachable, through
# the same rule rendered below otherwise. Re-run the installer after updating
# the playbook. Skip it once yourself with: git commit --no-verify
checker=${checker}
if command -v python3 >/dev/null 2>&1 && [[ -f "\${checker}" ]] \\
    && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  exec python3 "\${checker}" "\$1"
fi
if grep -qiE ${fallback} -- "\$1"; then
  echo "[playbook] AI attribution detected in the commit message." >&2
  echo "[playbook] Remove the Co-Authored-By / \"Generated with\" line and retry." >&2
  exit 1
fi
exit 0
HOOK
)"

  install_hook 'pre-commit' "$(cat <<'HOOK'
#!/usr/bin/env bash
# engineering-playbook hook — validation gate
# Approximates the playbook's Stop validation gate: no commit with a broken
# typecheck or lint. Skip it once yourself with: git commit --no-verify
# (no environment variable does it: the hook inherits the environment of the
# `git commit` that runs it, so the agent could set one on its own command).
fail=0
if [[ -f tsconfig.json ]] && command -v npx >/dev/null 2>&1; then
  echo "[playbook] tsc --noEmit"
  npx tsc --noEmit || fail=1
fi
if [[ -f package.json ]] && command -v npm >/dev/null 2>&1; then
  if grep -q '"lint:check"' package.json; then
    echo "[playbook] npm run lint:check"
    npm run lint:check || fail=1
  elif grep -q '"lint"' package.json; then
    echo "[playbook] npm run lint"
    npm run lint || fail=1
  fi
fi
if [[ "${fail}" -ne 0 ]]; then
  echo "[playbook] validation failed — commit blocked." >&2
  echo "[playbook] Fix the errors and commit again." >&2
  exit 1
fi
exit 0
HOOK
)"
fi

# 5. Optional CI workflow — the server-side copy of both hooks. The attribution
#    rule is rendered from playbook_core at install time, so it is the same
#    rule; re-run the installer after updating the playbook.
CI_MARK='# engineering-playbook ci'
if [[ "${CI}" -eq 1 ]]; then
  ci_path="${TARGET}/.github/workflows/engineering-playbook.yml"
  if [[ -f "${ci_path}" ]] && ! grep -qF "${CI_MARK}" "${ci_path}"; then
    echo "ci workflow: ${ci_path} exists and is not ours, NOT overwritten" >&2
  else
    rendered="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import playbook_core; print(open(sys.argv[2], encoding="utf-8").read().replace("@@RULE@@", playbook_core.AI_ATTRIBUTION_RE.pattern), end="")' \
      "${REPO_ROOT}/hooks" "${ADAPTER_DIR}/ci/engineering-playbook.yml")" || {
      echo "error: --ci needs python3 3.10+ to render the attribution rule" >&2
      exit 1
    }
    mkdir -p "$(dirname "${ci_path}")"
    printf '%s\n%s\n' "${CI_MARK} — generated by the Engineering Playbook's Antigravity installer (--ci)." \
      "${rendered}" > "${ci_path}"
    echo "ci workflow installed: ${ci_path}"
  fi
fi

TARGET_ABS="$(cd "${TARGET}" && pwd)"
cat <<EOF

done.

  Workspace:      ${TARGET_ABS}
  Skills:         ${SKILLS_DST}
  Workflows:      ${WORKFLOWS_DST}
  Slash commands: ${commands[*]}

NEXT STEP (required): reload the workspace in Antigravity.
  Antigravity indexes .agents/ only when the workspace is opened. If it is
  already open, run "Developer: Reload Window" (or reopen the folder) — until
  then, typing /engineering-playbook will NOT find the command.

Verify the files landed:
  ls "${WORKFLOWS_DST}"
Then in Antigravity type "/" — ${commands[*]} must appear in the menu.
EOF
