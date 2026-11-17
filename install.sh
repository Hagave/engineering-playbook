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

install_claude() {
  local version="$1" source="${REPO}"
  need claude "Install Claude Code first: https://code.claude.com"
  [[ -n "${version}" ]] && source="${REPO}#v${version}"
  claude plugin marketplace add "${source}"
  claude plugin marketplace update "${MARKETPLACE}"
  claude plugin install "${PLUGIN}@${MARKETPLACE}"
  claude plugin update "${PLUGIN}@${MARKETPLACE}"
  say "installed. Restart Claude Code to load it."
  say "updates: run this installer again, or turn on auto-update once in"
  say "  /plugin -> Marketplaces -> ${MARKETPLACE} -> Enable auto-update"
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
USAGE
}

main() {
  local harness="" target="" version="" download_only=0
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
      -h|--help) usage; return 0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
  done
  [[ ${#antigravity_flags[@]} -eq 0 || "${harness}" == "antigravity" ]] \
    || die "--git-hooks and --ci apply to --antigravity only."
  [[ -z "${version}" || "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || die "version '${version}' is not X.Y.Z."

  if [[ "${download_only}" -eq 0 && "${harness:-claude}" == "claude" ]]; then
    install_claude "${version}"
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
