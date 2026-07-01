#!/bin/bash
#
# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.
#
# Serve the generated Emporia Hydro dashboard over local HTTP. Runs until
# interrupted with Ctrl-C. Extra arguments (e.g. --port 9000) are forwarded to
# the emporia_hydro serve command.
#
# Usage: scripts/serve-reports.sh [--port PORT] [-h|--help]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly ROOT_DIR
readonly VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"

err() {
  echo "[serve-reports] ERROR: $*" >&2
}

die() {
  err "$*"
  exit 1
}

usage() {
  cat <<EOF
Usage: scripts/serve-reports.sh [--port PORT] [-h|--help]

Serve the generated dashboard (reports/) over local HTTP until Ctrl-C. Any
extra arguments are forwarded to 'emporia_hydro serve' (e.g. --port 9000).
EOF
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  [[ -x "${VENV_PYTHON}" ]] || die "venv python not found at ${VENV_PYTHON}."

  cd "${ROOT_DIR}" || die "Cannot cd to ${ROOT_DIR}"
  export PYTHONPATH="${ROOT_DIR}"

  if [[ ! -f "${ROOT_DIR}/reports/index.html" ]]; then
    die "No reports found in reports/. Run scripts/run.sh first."
  fi

  echo "[serve-reports] Serving reports/ (Ctrl-C to stop)..."
  exec "${VENV_PYTHON}" -m emporia_hydro serve "$@"
}

main "$@"
