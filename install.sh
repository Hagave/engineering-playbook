#!/usr/bin/env bash
# Engineering Playbook installer. Run it again to update.
#
#   curl -fsSL https://raw.githubusercontent.com/Hagave/engineering-playbook/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --codex
#   curl -fsSL .../install.sh | bash -s -- --antigravity ~/code/my-app --git-hooks --ci
#
# Harness (default: --claude):
#   --claude              Claude Code: adds this repository's plugin marketplace
#                         and installs the plugin through the `claude` CLI.
#   --codex               Codex CLI: downloads a release, then runs
#                         scripts/install-codex.sh from it.
#   --antigravity DIR     Google Antigravity: downloads a release, then installs
#                         the playbook into the workspace DIR.
#     --git-hooks, --ci   passed to the Antigravity installer.
#
# Claude Code accounts. Claude Code keeps its plugins inside each config
# directory, so someone with several accounts on one machine (one
# CLAUDE_CONFIG_DIR each) needs the plugin in every one of them. The installer
# finds ~/.claude, the ~/.claude-*/ directories that hold Claude Code state and
# the CLAUDE_CONFIG_DIR values set in shell startup files (read as text, never
# run), then asks on the terminal which of them to install into. The answer is
# kept in DIR/claude-accounts, so a later run updates the same accounts and only
# asks about an account it has not seen. Without a terminal to ask on, only the
# current account is installed. A startup-file path with a space in it is not
# detected; name that account with --accounts.
#   --all-accounts        install into every account found, without asking.
#   --accounts LIST       install into exactly these config directories
#                         (comma-separated; "default" is ~/.claude).
#
# Other options:
#   --version X.Y.Z       install that release instead of the latest.
#   --dir DIR             where releases are kept (default:
#                         ${XDG_DATA_HOME:-~/.local/share}/engineering-playbook).
#   --download-only       download and verify a release, print its path, stop.
#
# A release is downloaded as the archive its release workflow built, checked
# against the SHA-256 published next to it, unpacked into DIR/<version> and
# linked as DIR/current. The Codex and Antigravity installs point at
# DIR/current, so an update only moves the link. Nothing runs with sudo.
#
# The whole script is a function called on the last line, so a download cut
# short runs nothing.

set -euo pipefail

REPO="${PLAYBOOK_INSTALL_REPO:-Hagave/engineering-playbook}"
RELEASES_URL="${PLAYBOOK_INSTALL_RELEASES_URL:-https://github.com/${REPO}/releases/download}"
API_URL="${PLAYBOOK_INSTALL_API_URL:-https://api.github.com}"
MARKETPLACE="engineering-playbook"
PLUGIN="engineering-playbook"

say() { printf '[playbook] %s\n' "$*"; }
die() { printf '[playbook] error: %s\n' "$*" >&2; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required. $2"
}

need_python() {
  need python3 "Install Python 3.10 or newer."
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "python3 is $(python3 -c 'import platform; print(platform.python_version())'); the playbook's hooks need 3.10 or newer."
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    die "sha256sum or shasum is required to verify the download."
  fi
}

latest_version() {
  local json
  json="$(curl -fsSL "${API_URL}/repos/${REPO}/releases/latest")" \
    || die "could not ask GitHub for the latest release of ${REPO}."
  printf '%s' "${json}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))' \
    || die "could not read the latest release's tag."
}

# Download, verify and unpack release $1 into $2/$1; link $2/current to it.
fetch_release() {
  local version="$1" dir="$2"
  local asset="engineering-playbook-${version}.tar.gz"
  local url="${RELEASES_URL}/v${version}/${asset}"
  [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version '${version}' is not X.Y.Z."
  mkdir -p "${dir}"

  if [[ -f "${dir}/${version}/.complete" ]]; then
    say "release ${version} is already in ${dir}/${version}"
  else
    local tmp
    tmp="$(mktemp -d "${dir}/.download-XXXXXX")"
    # shellcheck disable=SC2064  # expand now: tmp is local to this function
    trap "rm -rf '${tmp}'" EXIT
    say "downloading ${asset}"
    curl -fsSL -o "${tmp}/${asset}" "${url}" \
      || die "could not download ${url}. Releases before 4.0.2 have no installer archive; pass --version 4.0.2 or newer."
    curl -fsSL -o "${tmp}/${asset}.sha256" "${url}.sha256" \
      || die "could not download the checksum ${url}.sha256."
    local expected actual
    expected="$(cut -d' ' -f1 "${tmp}/${asset}.sha256")"
    actual="$(sha256_of "${tmp}/${asset}")"
    [[ -n "${expected}" && "${expected}" == "${actual}" ]] \
      || die "checksum mismatch for ${asset} (expected ${expected:-nothing}, got ${actual}). Nothing was installed."
    tar -xzf "${tmp}/${asset}" -C "${tmp}"
    [[ -d "${tmp}/engineering-playbook-${version}" ]] \
      || die "${asset} does not hold engineering-playbook-${version}/."
    rm -rf "${dir:?}/${version}"
    mv "${tmp}/engineering-playbook-${version}" "${dir}/${version}"
    touch "${dir}/${version}/.complete"
    rm -rf "${tmp}"
    trap - EXIT
  fi
  ln -sfn "${version}" "${dir}/current"
  say "release ${version} is linked as ${dir}/current"
}

# ── Claude Code accounts ──────────────────────────────────────────────────────
# An account is "default" (~/.claude, run with CLAUDE_CONFIG_DIR unset) or the
# absolute path of its config directory. The default account is run with the
# variable unset rather than pointed at ~/.claude: with the variable set,
# Claude Code reads its global state from inside the directory instead of
# ~/.claude.json. This script keeps to bash 3.2 (macOS): no mapfile, no
# associative arrays.

CLAUDE_ACCOUNTS=()

# ~/.claude (with or without a trailing slash) is "default"; others unchanged.
normalize_account() {
  local p="${1%/}"
  if [[ -z "${p}" || "${p}" == "${HOME}/.claude" ]]; then
    printf 'default'
  else
    printf '%s' "${p}"
  fi
}

account_label() {
  if [[ "$1" == "default" ]]; then
    printf '%s' "${HOME}/.claude (default)"
  else
    printf '%s' "$1"
  fi
}

# A path written in a startup file, made absolute without running anything:
# only ~/, $HOME/ and ${HOME}/ are expanded. Anything else that would need the
# shell (another variable, a command substitution) is refused.
expand_home() {
  local v="$1"
  case "${v}" in
    '~'/*) v="${HOME}/${v#'~/'}" ;;
    '$HOME'/*) v="${HOME}/${v#'$HOME/'}" ;;
    '${HOME}'/*) v="${HOME}/${v#'${HOME}/'}" ;;
  esac
  [[ "${v}" == /* && "${v}" != *'$'* && "${v}" != *'`'* ]] || return 1
  printf '%s' "${v}"
}

# A directory Claude Code has used: it holds its state, settings or sessions.
is_config_dir() {
  [[ -f "$1/.claude.json" || -f "$1/.credentials.json" || -f "$1/settings.json" || -d "$1/projects" ]]
}

# Accounts on this machine, one per line, the current one first.
detect_claude_accounts() {
  local current d rc value
  current="$(normalize_account "${CLAUDE_CONFIG_DIR:-}")"
  {
    printf '%s\n' "${current}"
    [[ -d "${HOME}/.claude" ]] && printf 'default\n'
    for d in "${HOME}"/.claude-*/; do
      [[ -d "${d}" ]] && is_config_dir "${d%/}" && normalize_account "${d%/}" && printf '\n'
    done
    for rc in "${HOME}/.zshrc" "${HOME}/.zshenv" "${HOME}/.bashrc" "${HOME}/.bash_aliases" \
              "${HOME}/.bash_profile" "${HOME}/.profile" "${HOME}/.config/fish/config.fish"; do
      [[ -f "${rc}" ]] || continue
      while IFS= read -r value; do
        value="$(expand_home "${value}")" || continue
        [[ -d "${value}" ]] && is_config_dir "${value}" && normalize_account "${value}" && printf '\n'
      done < <(grep -oE "CLAUDE_CONFIG_DIR[= ]+[\"']?[^\"' ;&|)]+" "${rc}" \
                 | sed -E "s/^CLAUDE_CONFIG_DIR[= ]+[\"']?//")
    done
    true  # a failed test above is not a failure of the listing (pipefail)
  } | awk 'NF && !seen[$0]++'
}

add_account() {
  local a
  for a in ${CLAUDE_ACCOUNTS[@]+"${CLAUDE_ACCOUNTS[@]}"}; do
    [[ "${a}" == "$1" ]] && return 0
  done
  CLAUDE_ACCOUNTS+=("$1")
}

# curl | bash feeds the script itself on stdin, so the answer is read from the
# terminal on fd 3. PLAYBOOK_INSTALL_TTY lets the tests answer from a file, or
# name a missing path to mean "no terminal".
open_tty() {
  { exec 3<"${PLAYBOOK_INSTALL_TTY:-/dev/tty}"; } 2>/dev/null
}

# Ask which of the accounts in $@ to install into; prints "+acct" or "-acct"
# for each one. The question goes to stderr: stdout carries the answers.
ask_accounts() {
  local i=1 a answer n
  say "Claude Code keeps plugins per account. Also found on this machine:" >&2
  for a in "$@"; do
    say "  ${i}) $(account_label "${a}")" >&2
    i=$((i + 1))
  done
  printf '[playbook] Install into them too? [a]ll, [n]one, or numbers like 1,3 [a]: ' >&2
  IFS= read -r answer <&3 || answer=""
  answer="$(printf '%s' "${answer}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
  case "${answer}" in
    ''|a|all) for a in "$@"; do printf '+%s\n' "${a}"; done; return 0 ;;
    n|none)   for a in "$@"; do printf -- '-%s\n' "${a}"; done; return 0 ;;
  esac
  # No leading zeros: bash arithmetic reads 01 as octal, and the match below
  # compares the digits as written.
  [[ "${answer}" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]] || die "answer '${answer}' not understood: use a, n or numbers like 1,3."
  for n in ${answer//,/ }; do
    (( n >= 1 && n <= $# )) || die "there is no account number ${n}."
  done
  i=1
  for a in "$@"; do
    if [[ ",${answer}," == *",${i},"* ]]; then printf '+%s\n' "${a}"; else printf -- '-%s\n' "${a}"; fi
    i=$((i + 1))
  done
}

# Fill CLAUDE_ACCOUNTS. $1 is "list", "all" or "ask"; $2 the --accounts list;
# $3 the file that keeps the answers ("+acct" installed, "-acct" declined).
choose_claude_accounts() {
  local mode="$1" list="$2" state="$3" detected acct current saved="" answers
  local others=()
  case "${mode}" in
    list)
      local entries=() entry
      IFS=',' read -r -a entries <<<"${list}"
      for entry in ${entries[@]+"${entries[@]}"}; do
        [[ -n "${entry}" ]] || continue
        acct="${entry}"
        if [[ "${entry}" != "default" ]]; then
          acct="$(expand_home "${entry}")" || die "--accounts: '${entry}' is not an absolute path."
          [[ -d "${acct}" ]] || die "--accounts: ${acct} is not a directory."
        fi
        add_account "$(normalize_account "${acct}")"
      done
      [[ ${#CLAUDE_ACCOUNTS[@]} -gt 0 ]] || die "--accounts needs at least one config directory."
      return 0 ;;
    all)
      while IFS= read -r acct; do add_account "${acct}"; done < <(detect_claude_accounts)
      return 0 ;;
  esac

  detected="$(detect_claude_accounts)"
  current="$(printf '%s\n' "${detected}" | head -n 1)"
  add_account "${current}"
  [[ -f "${state}" ]] && saved="$(cat "${state}")"
  while IFS= read -r acct; do
    [[ -n "${acct}" && "${acct}" != "${current}" ]] || continue
    if printf '%s\n' "${saved}" | grep -qxF -- "+${acct}"; then
      add_account "${acct}"
    elif ! printf '%s\n' "${saved}" | grep -qxF -- "-${acct}"; then
      others+=("${acct}")
    fi
  done <<<"${detected}"
  [[ ${#others[@]} -gt 0 ]] || return 0

  if ! open_tty; then
    say "${#others[@]} more Claude Code account(s) found; installing only into $(account_label "${current}")."
    say "  To include them, run this again in a terminal, or pass --all-accounts."
    return 0
  fi
  answers="$(ask_accounts "${others[@]}")"
  exec 3<&-
  while IFS= read -r acct; do
    [[ "${acct}" == +* ]] && add_account "${acct#+}"
  done <<<"${answers}"
  { printf '%s\n' "${saved}"; printf '%s\n' "${answers}"; } | awk 'NF && !seen[$0]++' > "${state}"
}

# Run the claude CLI as account $1.
claude_in() {
  local account="$1"
  shift
  if [[ "${account}" == "default" ]]; then
    env -u CLAUDE_CONFIG_DIR claude "$@"
  else
    CLAUDE_CONFIG_DIR="${account}" claude "$@"
  fi
}

install_claude_account() {
  local account="$1" source="$2"
  claude_in "${account}" plugin marketplace add "${source}" || return 1
  claude_in "${account}" plugin marketplace update "${MARKETPLACE}" || return 1
  claude_in "${account}" plugin install "${PLUGIN}@${MARKETPLACE}" || return 1
  claude_in "${account}" plugin update "${PLUGIN}@${MARKETPLACE}" || return 1
}

install_claude() {
  local version="$1" mode="$2" list="$3" dir="$4" source="${REPO}" account
  local failed=()
  need claude "Install Claude Code first: https://code.claude.com"
  [[ -n "${version}" ]] && source="${REPO}#v${version}"
  mkdir -p "${dir}"
  choose_claude_accounts "${mode}" "${list}" "${dir}/claude-accounts"
  for account in "${CLAUDE_ACCOUNTS[@]}"; do
    [[ ${#CLAUDE_ACCOUNTS[@]} -gt 1 ]] && say "account $(account_label "${account}")"
    install_claude_account "${account}" "${source}" || failed+=("$(account_label "${account}")")
  done
  if [[ ${#failed[@]} -gt 0 ]]; then
    die "the plugin could not be installed into: ${failed[*]}. Run this again to retry."
  fi
  say "installed. Restart Claude Code to load it."
  say "updates: run this installer again, or turn on auto-update once in"
  say "  /plugin -> Marketplaces -> ${MARKETPLACE} -> Enable auto-update"
  [[ ${#CLAUDE_ACCOUNTS[@]} -gt 1 ]] && say "  (auto-update is set per account: turn it on in each one)"
  return 0
}

# Print a short first-run guide, once per install dir. The marker records that
# the playbook was installed here before, so an update stays quiet.
maybe_tour() {
  local harness="$1" dir="$2"
  mkdir -p "${dir}"
  local marker="${dir}/.installed"
  [[ -e "${marker}" ]] && return 0
  : > "${marker}"
  say ""
  say "First install -- quick start:"
  case "${harness}" in
    claude)
      say "  - Open or restart Claude Code; the playbook loads at session start."
      say "  - /playbook-config          settings, status and the output-filter totals"
      say "  - /playbook-off, /playbook-on   suspend or resume the rules for a session"
      ;;
    *)
      say "  - The playbook is installed under ${dir}/current."
      say "  - Start a new ${harness} session; the rules load from there."
      ;;
  esac
  say "  - Update anytime: run this same command again."
}

usage() {
  cat <<'USAGE'
Engineering Playbook installer. Run it again to update.

  install.sh [--claude]                                   Claude Code (default)
  install.sh --codex                                      Codex CLI
  install.sh --antigravity DIR [--git-hooks] [--ci]       Google Antigravity workspace DIR

  --version X.Y.Z   install that release instead of the latest
  --dir DIR         where releases are kept (default: ~/.local/share/engineering-playbook)
  --download-only   download and verify a release, print its path, stop

  Claude Code with several accounts (one CLAUDE_CONFIG_DIR each):
  --all-accounts    install into every account found, without asking
  --accounts LIST   install into these config directories (comma-separated;
                    "default" is ~/.claude)
USAGE
}

main() {
  local harness="" target="" version="" download_only=0
  local accounts_mode="ask" accounts_list=""
  local dir="${XDG_DATA_HOME:-${HOME}/.local/share}/engineering-playbook"
  local antigravity_flags=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --claude) harness="claude" ;;
      --codex) harness="codex" ;;
      --antigravity)
        harness="antigravity"
        [[ $# -ge 2 ]] || die "--antigravity needs the workspace directory."
        target="$2"; shift ;;
      --git-hooks|--ci) antigravity_flags+=("$1") ;;
      --version)
        [[ $# -ge 2 ]] || die "--version needs X.Y.Z."
        version="${2#v}"; shift ;;
      --dir)
        [[ $# -ge 2 ]] || die "--dir needs a directory."
        dir="$2"; shift ;;
      --download-only) download_only=1 ;;
      --all-accounts)
        [[ "${accounts_mode}" != "list" ]] || die "use --all-accounts or --accounts, not both."
        accounts_mode="all" ;;
      --accounts)
        [[ $# -ge 2 ]] || die "--accounts needs a comma-separated list of config directories."
        [[ "${accounts_mode}" != "all" ]] || die "use --all-accounts or --accounts, not both."
        accounts_mode="list"; accounts_list="$2"; shift ;;
      -h|--help) usage; return 0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
  done
  [[ ${#antigravity_flags[@]} -eq 0 || "${harness}" == "antigravity" ]] \
    || die "--git-hooks and --ci apply to --antigravity only."
  [[ -z "${version}" || "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || die "version '${version}' is not X.Y.Z."
  [[ "${accounts_mode}" == "ask" || ( "${harness:-claude}" == "claude" && "${download_only}" -eq 0 ) ]] \
    || die "--all-accounts and --accounts apply to a Claude Code install only."

  if [[ "${download_only}" -eq 0 && "${harness:-claude}" == "claude" ]]; then
    install_claude "${version}" "${accounts_mode}" "${accounts_list}" "${dir}"
    maybe_tour "claude" "${dir}"
    return 0
  fi

  need curl "Install curl."
  need tar "Install tar."
  need_python
  [[ -n "${version}" ]] || version="$(latest_version)"
  fetch_release "${version}" "${dir}"
  local root="${dir}/current"

  if [[ "${download_only}" -eq 1 ]]; then
    printf '%s\n' "${root}"
    return 0
  fi
  case "${harness}" in
    codex)
      bash "${root}/scripts/install-codex.sh"
      ;;
    antigravity)
      [[ -d "${target}" ]] || die "workspace ${target} does not exist."
      bash "${root}/adapters/antigravity/install.sh" "${antigravity_flags[@]+"${antigravity_flags[@]}"}" "${target}"
      ;;
  esac
  maybe_tour "${harness}" "${dir}"
  say "done. Run the same command again to update."
}

main "$@"
