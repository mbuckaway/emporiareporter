#!/bin/bash
#
# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.
#
# Serve the generated Emporia Hydro dashboard over local HTTP and open it in the
# default browser. Serves on port 8765 by default and runs until interrupted
# with Ctrl-C.
#
# Usage: scripts/serve-reports.sh [--port PORT] [--no-browser] [-h|--help]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly ROOT_DIR
readonly VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"
readonly DEFAULT_PORT=8765

err() {
  echo "[serve-reports] ERROR: $*" >&2
}

die() {
  err "$*"
  exit 1
}

usage() {
  cat <<EOF
Usage: scripts/serve-reports.sh [--port PORT] [--no-browser] [-h|--help]

Serve the generated dashboard (reports/) over local HTTP and open the index
page in your browser. Runs until Ctrl-C.

Options:
  --port PORT   Port to serve on (default: ${DEFAULT_PORT}).
  --no-browser  Do not open a browser; just serve.
  -h, --help    Show this help message.
EOF
}

#######################################
# Wait until a TCP port accepts a local connection, or time out (~10s).
# Arguments: port
# Returns: 0 once the port is listening, 1 on timeout
#######################################
wait_for_port() {
  local port="$1"
  local attempt
  for (( attempt = 0; attempt < 50; attempt++ )); do
    if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

#######################################
# Open a URL in the default browser (macOS 'open' or Linux 'xdg-open').
# Arguments: url
#######################################
open_url() {
  local url="$1"
  if command -v open > /dev/null 2>&1; then
    open "${url}"
  elif command -v xdg-open > /dev/null 2>&1; then
    xdg-open "${url}"
  else
    echo "[serve-reports] Open ${url} in your browser."
  fi
}

#######################################
# Wait for the server to come up, then open the dashboard index.
# Arguments: port, url
#######################################
open_when_ready() {
  local port="$1"
  local url="$2"
  if wait_for_port "${port}"; then
    open_url "${url}"
  else
    err "Server did not start on port ${port}; open ${url} manually."
  fi
}

main() {
  local port="${DEFAULT_PORT}"
  local open_browser="true"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --port)
        [[ $# -ge 2 ]] || die "--port requires a value."
        port="$2"
        shift 2
        ;;
      --no-browser)
        open_browser="false"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        err "Unknown option: $1"
        usage
        exit 1
        ;;
    esac
  done

  [[ -x "${VENV_PYTHON}" ]] || die "venv python not found at ${VENV_PYTHON}."
  cd "${ROOT_DIR}" || die "Cannot cd to ${ROOT_DIR}"
  export PYTHONPATH="${ROOT_DIR}"

  if [[ ! -f "${ROOT_DIR}/reports/index.html" ]]; then
    die "No reports found in reports/. Run scripts/run.sh first."
  fi

  local url="http://127.0.0.1:${port}/index.html"
  echo "[serve-reports] Serving reports/ at ${url} (Ctrl-C to stop)..."

  # Open the browser once the server is accepting connections. Runs in the
  # background so the server can start in the foreground below.
  if [[ "${open_browser}" == "true" ]]; then
    open_when_ready "${port}" "${url}" &
  fi

  # Replace this shell with the server so Ctrl-C goes straight to it.
  exec "${VENV_PYTHON}" -m emporia_hydro serve --port "${port}"
}

main "$@"
