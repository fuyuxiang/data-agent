#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "$0")/.."
MERIDIAN_BUILD_ROOT="$(pwd -P)/build/desktop"
rm -rf -- "$MERIDIAN_BUILD_ROOT"
mkdir -p "$MERIDIAN_BUILD_ROOT"
python3 packaging/build_staging.py --source . --destination "$MERIDIAN_BUILD_ROOT/staging"
export MERIDIAN_STAGING_ROOT="$MERIDIAN_BUILD_ROOT/staging"
python3 -m PyInstaller --clean --noconfirm \
  --workpath "$MERIDIAN_BUILD_ROOT/work" --distpath "$MERIDIAN_BUILD_ROOT/dist" \
  packaging/meridian.spec
echo "Desktop package: $MERIDIAN_BUILD_ROOT/dist"
