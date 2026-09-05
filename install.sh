#!/usr/bin/env sh
set -eu

MERIDIAN_REPOSITORY="${MERIDIAN_REPOSITORY:-https://github.com/fuyuxiang/data-agent.git}"
MERIDIAN_INSTALL_ROOT="${MERIDIAN_INSTALL_ROOT:-$HOME/.local/share/meridian-analytics}"
MERIDIAN_LAUNCHER_ROOT="${MERIDIAN_LAUNCHER_ROOT:-$HOME/.local/bin}"

command -v git >/dev/null 2>&1 || { echo "[ERROR] Git is required." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "[ERROR] Python 3.10+ is required." >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "[ERROR] Python 3.10+ is required." >&2
  exit 1
}

mkdir -p "$MERIDIAN_INSTALL_ROOT" "$MERIDIAN_LAUNCHER_ROOT"
MERIDIAN_PROJECT="$MERIDIAN_INSTALL_ROOT/data-agent"
if [ -d "$MERIDIAN_PROJECT/.git" ]; then
  git -C "$MERIDIAN_PROJECT" pull --ff-only
elif [ -e "$MERIDIAN_PROJECT" ]; then
  echo "[ERROR] $MERIDIAN_PROJECT exists but is not a Git checkout." >&2
  exit 1
else
  git clone --depth 1 "$MERIDIAN_REPOSITORY" "$MERIDIAN_PROJECT"
fi

python3 -m venv "$MERIDIAN_PROJECT/.venv"
"$MERIDIAN_PROJECT/.venv/bin/python" -m pip install --disable-pip-version-check \
  --require-hashes -r "$MERIDIAN_PROJECT/requirements.lock"

MERIDIAN_LAUNCHER="$MERIDIAN_LAUNCHER_ROOT/meridian-analytics"
{
  echo '#!/usr/bin/env sh'
  printf 'cd -- %s\n' "$(printf %s "$MERIDIAN_PROJECT" | sed "s/'/'\\\\''/g; s/^/'/; s/$/'/")"
  echo 'exec .venv/bin/python app.py "$@"'
} > "$MERIDIAN_LAUNCHER"
chmod 0755 "$MERIDIAN_LAUNCHER"

echo "Installed. Run: $MERIDIAN_LAUNCHER"
