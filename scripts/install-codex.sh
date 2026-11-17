#!/usr/bin/env bash
# scripts/install-codex.sh — install the engineering-playbook plugin for Codex CLI.
#
# Uses Codex's official `codex plugin add` so the plugin is registered with
# Codex's plugin registry. Manual copies of plugin files are ignored — Codex
# only loads plugins it knows about through `codex plugin add`.
#
# Modes:
#   default (production)  — cache is a snapshot; edits in the repo do NOT
#                           propagate. Re-run this script to refresh.
#   --dev                  — after install, replaces the cache's hooks/ and
#                           skills/ with symlinks to the repo, so edits take
#                           effect on the next codex run.
#
# Other flags:
#   --refresh              — re-runs `codex plugin add` to refresh the cache
#   --uninstall            — removes the plugin via `codex plugin remove`
#   --clean                — removes leftover manual install dirs that Codex
#                           does not recognize (does not touch the active plugin)
#   --plugin NAME          — install a different plugin from the marketplace
#                            (default: playbook)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Resolve how to invoke Codex: a persistent `codex` binary on PATH, or fall
# back to `npx @openai/codex` for npx-only installs (no binary left on PATH
# between runs).
if command -v codex >/dev/null 2>&1; then
  CODEX_CMD=(codex)
elif command -v npx >/dev/null 2>&1; then
  CODEX_CMD=(npx --yes @openai/codex)
else
  echo "install-codex.sh: neither 'codex' nor 'npx' found on PATH." >&2
  exit 1
fi

MARKETPLACE="playbook"
PLUGIN_NAME="playbook"
PLUGIN_KEY="${PLUGIN_NAME}@${MARKETPLACE}"
MODE="production"
ACTION="install"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) MODE="dev"; shift ;;
    --refresh) ACTION="refresh"; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    --clean) ACTION="clean"; shift ;;
    --plugin) PLUGIN_NAME="$2"; PLUGIN_KEY="${PLUGIN_NAME}@${MARKETPLACE}"; shift 2 ;;
    -h|--help)
      sed -n '2,23p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) echo "install-codex.sh: unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Ensure marketplace is registered. `codex plugin marketplace add` takes a
# single SOURCE argument (verified against Codex 0.147.0) — the marketplace
# name comes from .agents/plugins/marketplace.json's own "name" field, not
# from a second CLI argument.
if ! "${CODEX_CMD[@]}" plugin marketplace list 2>/dev/null | grep -q "^${MARKETPLACE} "; then
  echo "install-codex.sh: registering marketplace ${MARKETPLACE} -> ${REPO_ROOT}"
  "${CODEX_CMD[@]}" plugin marketplace add "${REPO_ROOT}"
fi

case "${ACTION}" in
  uninstall)
    echo "install-codex.sh: uninstalling ${PLUGIN_KEY}"
    "${CODEX_CMD[@]}" plugin remove "${PLUGIN_KEY}" 2>/dev/null || true
    echo "done. restart Codex for changes to take effect."
    exit 0
    ;;
  clean)
    for stale in "${HOME}/.codex/plugins/${PLUGIN_NAME}" "${HOME}/.codex/plugins/${PLUGIN_NAME}-dev"; do
      if [[ -d "${stale}" ]]; then
        echo "install-codex.sh: removing leftover ${stale}"
        rm -rf "${stale}"
      fi
    done
    echo "done."
    exit 0
    ;;
esac

# Read the plugin's declared version from the repo (source of truth). The
# cache path is derived from this via the standard codex convention
# ${HOME}/.codex/plugins/cache/${marketplaceName}/${name}/${version}. The
# previous implementation parsed `codex plugin list --json` and fell back
# to a hardcoded "3.0.0"; that broke every time the version bumped because
# the JSON schema does not expose `installedPath` (the cache path is
# constructed by convention, not stored in the listing).
PLUGIN_VERSION="$(python3 -c "import json,sys; print(json.load(open('${REPO_ROOT}/.codex-plugin/plugin.json'))['version'])" 2>/dev/null || true)"
if [[ -z "${PLUGIN_VERSION}" ]]; then
  echo "install-codex.sh: cannot read version from ${REPO_ROOT}/.codex-plugin/plugin.json" >&2
  exit 1
fi

# Install / refresh.
echo "install-codex.sh: ${ACTION}ing ${PLUGIN_KEY}"
"${CODEX_CMD[@]}" plugin add "${PLUGIN_KEY}"

# Resolve cache path by convention from the repo's declared version.
CACHE_PATH="${HOME}/.codex/plugins/cache/${MARKETPLACE}/${PLUGIN_NAME}/${PLUGIN_VERSION}"

# Verify the cache actually exists after install. If `codex plugin add`
# reported success but the cache is missing, abort early with a clear
# error rather than verifying the wrong path.
if [[ ! -d "${CACHE_PATH}" ]]; then
  echo "install-codex.sh: cache directory not found at ${CACHE_PATH}" >&2
  echo "  codex plugin add reported success but the cache is missing." >&2
  echo "  Check that codex has write access to ${HOME}/.codex/plugins/cache/${MARKETPLACE}/${PLUGIN_NAME}/" >&2
  exit 1
fi
echo "install-codex.sh: cache at ${CACHE_PATH}"

# Verify hooks.json wires all 7 events.
python3 - "${CACHE_PATH}/.codex-plugin/hooks.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
events = set((data.get('hooks') or {}).keys())
required = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'Stop', 'SubagentStop', 'PreCompact', 'PostCompact')
missing = {e for e in required if e not in events}
if missing:
    print(f'install-codex.sh: hooks.json missing events {sorted(missing)}', file=sys.stderr)
    sys.exit(1)
print(f'install-codex.sh: hooks.json wires all {len(required)} events')
PY

# In dev mode, replace the cache's hooks/ and skills/ with symlinks.
if [[ "${MODE}" == "dev" ]]; then
  echo "install-codex.sh: dev mode — symlinking hooks/ and skills/ to repo"
  for sub in hooks skills; do
    target="${CACHE_PATH}/${sub}"
    rm -rf "${target}"
    ln -sfn "${REPO_ROOT}/${sub}" "${target}"
    echo "  ${target} -> ${REPO_ROOT}/${sub}"
  done
fi

# Sanity check the global Codex hooks config for entries that target a
# different LLM harness: a third-party CLI whose Codex installer registers
# its `<tool> hook claude` command -- a CLAUDE CODE hook processor -- into
# Codex's global hooks.json. Claude Code's hook I/O
# contract differs from Codex's; Codex 0.147.0 rejects the foreign payload
# with: "PreToolUse hook returned updatedInput without permissionDecision:
# allow". Detect any such entries and warn loudly.
GLOBAL_HOOKS_FILE="${HOME}/.codex/hooks.json"
if [[ -f "${GLOBAL_HOOKS_FILE}" ]]; then
  # `<tool> hook <provider>` is how such CLIs name a per-harness hook
  # processor. The providers below are other harnesses; none of them is
  # valid in Codex's hooks.json, so any match is a foreign harness.
  FOREIGN_HOOKS=$(grep -E ' hook (claude|cursor|gemini|copilot)["]' "${GLOBAL_HOOKS_FILE}" 2>/dev/null || true)
  if [[ -n "${FOREIGN_HOOKS}" ]]; then
    cat >&2 <<'WARN'
install-codex.sh: WARNING -- foreign-harness hook detected in ~/.codex/hooks.json

  A third-party tool registered a hook processor for a
  DIFFERENT LLM harness into Codex's global hooks config. Codex 0.147.0
  will reject these hooks at runtime with:

    PreToolUse hook returned updatedInput without permissionDecision:allow

  Offending entries:
WARN
    echo "${FOREIGN_HOOKS}" | sed 's/^/    /' >&2
    cat >&2 <<'WARN'

  To fix (pick one):
    1. Remove the offending entry from ~/.codex/hooks.json manually.
    2. Uninstall that tool's Codex integration with its own uninstall
       command (check ~/.codex/AGENTS.md for what else it wrote).

  This warning is non-fatal so the install can still complete. Removing
  the bad hook is the user's responsibility. See docs/adapters/codex.md
  "Known conflicts" for the full backstory.
WARN
  fi
fi

cat <<EOF

done. restart Codex for changes to take effect.

  Plugin:   ${PLUGIN_KEY}
  Source:   ${REPO_ROOT}
  Cache:    ${CACHE_PATH}
  Mode:     ${MODE}
EOF
