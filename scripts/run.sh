#!/bin/bash
#
# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.
#
# Generate the Emporia Hydro dashboard reports. When Emporia credentials are
# present it first pulls the latest usage from the cloud, then builds the HTML
# report set (report page, index, year-to-date, charts) into reports/.
#
# Usage: scripts/run.sh [--no-pull] [-h|--help]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly ROOT_DIR
readonly VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"

log() {
  echo "[run] $*"
}

err() {
  echo "[run] ERROR: $*" >&2
}

die() {
  err "$*"
  exit 1
}

usage() {
  cat <<EOF
Usage: scripts/run.sh [--no-pull] [-h|--help]

Generate the Emporia Hydro dashboard reports. By default pulls the latest
usage from the Emporia cloud (when config/keys.json or config/token_cache.json
exists), then builds the reports into reports/.

Options:
  --no-pull   Skip the cloud pull; build reports from the cached usage only.
  -h, --help  Show this help message.
EOF
}

#######################################
# Pull the latest usage when credentials exist. A pull failure is a warning,
# not fatal, so an existing cache can still be reported.
# Globals: ROOT_DIR, VENV_PYTHON
#######################################
pull_usage() {
  local keys="${ROOT_DIR}/config/keys.json"
  local tokens="${ROOT_DIR}/config/token_cache.json"
  if [[ ! -f "${keys}" && ! -f "${tokens}" ]]; then
    log "No credentials found; skipping pull (using cached usage)."
    return 0
  fi
  log "Pulling latest usage from the Emporia cloud..."
  if ! "${VENV_PYTHON}" -m emporia_hydro pull; then
    err "Pull failed; continuing with cached usage if available."
  fi
}

main() {
  local do_pull="true"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-pull) do_pull="false"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) err "Unknown option: $1"; usage; exit 1 ;;
    esac
  done

  if [[ ! -x "${VENV_PYTHON}" ]]; then
    err "venv python not found at ${VENV_PYTHON}."
    err "Create the venv: python3.14 -m venv .venv"
    err "Then install:    .venv/bin/pip install -e '.[dev]'"
    exit 1
  fi

  cd "${ROOT_DIR}" || die "Cannot cd to ${ROOT_DIR}"
  export PYTHONPATH="${ROOT_DIR}"

  if [[ "${do_pull}" == "true" ]]; then
    pull_usage
  else
    log "Skipping pull (--no-pull); building from cached usage."
  fi

  log "Generating reports..."
  if ! "${VENV_PYTHON}" -m emporia_hydro report; then
    die "Report generation failed."
  fi

  log "Done. Open reports/index.html or run scripts/serve-reports.sh to view."
}

main "$@"
