#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "$0")"

if command -v python3 >/dev/null 2>&1; then
  MERIDIAN_PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  MERIDIAN_PYTHON=python
else
  echo "[ERROR] Python 3.10+ is required." >&2
  exit 1
fi

"$MERIDIAN_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "[ERROR] Python 3.10+ is required." >&2
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  "$MERIDIAN_PYTHON" -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check --require-hashes -r requirements.lock
exec .venv/bin/python app.py
