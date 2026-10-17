#!/usr/bin/env bash
# 本机开发用：构建 Release 版 BHE.app 并替换 /Applications 里的旧版本。
#
# 默认复用 dist/macos-runtime 里已经准备好的 Python、FFmpeg 和模型，只把仓库
# 当前的 engine/src/scripts/数据库 schema 同步进去，这样每次改动后几分钟内就能
# 装好新版本。需要整体重建运行时（例如改了 requirements.txt）时：
#
#   BHE_FULL_RUNTIME=1 BHE_PYTHON_RUNTIME=/path/to/portable-python \
#     scripts/install_macos_app.sh
#
# 其他环境变量：
#   FLUTTER_BIN          Flutter 可执行文件，默认取 PATH 中的 flutter
#   BHE_INSTALL_DIR      安装目录，默认 /Applications
#   BHE_SKIP_TESTS=1     跳过 flutter test / pytest
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_OUT="${BHE_RUNTIME_OUT:-$ROOT/dist/macos-runtime}"
INSTALL_DIR="${BHE_INSTALL_DIR:-/Applications}"
APP_NAME="${BHE_APP_NAME:-BHE}"
BUILT_APP="$ROOT/apps/desktop/build/macos/Build/Products/Release/$APP_NAME.app"
TARGET_APP="$INSTALL_DIR/$APP_NAME.app"
FLUTTER="${FLUTTER_BIN:-$(command -v flutter || true)}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install_macos_app.sh only runs on macOS" >&2
  exit 2
fi
if [[ -z "$FLUTTER" || ! -x "$FLUTTER" ]]; then
  echo "Flutter not found; install it or set FLUTTER_BIN" >&2
  exit 2
fi

if [[ "${BHE_SKIP_TESTS:-0}" != "1" ]]; then
  echo "==> Running tests"
  (cd "$ROOT/apps/desktop" && "$FLUTTER" test)
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    (cd "$ROOT" && .venv/bin/python -m pytest -q)
  else
    echo "Skipping pytest: .venv not found (see docs/DEVELOPMENT.md)" >&2
  fi
fi

if [[ "${BHE_FULL_RUNTIME:-0}" == "1" ]]; then
  echo "==> Rebuilding runtime from scratch"
  "$ROOT/scripts/prepare_macos_runtime.sh" "$RUNTIME_OUT"
else
  if [[ ! -x "$RUNTIME_OUT/python/bin/python3" || ! -f "$RUNTIME_OUT/models/bball_model.pt" ]]; then
    echo "No prepared runtime at $RUNTIME_OUT." >&2
    echo "Run once with BHE_FULL_RUNTIME=1 BHE_PYTHON_RUNTIME=... (see docs/RELEASE.md)." >&2
    exit 2
  fi
  echo "==> Syncing engine code into $RUNTIME_OUT"
  rsync -a --delete --exclude __pycache__ "$ROOT/engine/python/" "$RUNTIME_OUT/engine/python/"
  rsync -a --delete --exclude __pycache__ "$ROOT/src/" "$RUNTIME_OUT/src/"
  rsync -a --delete --exclude __pycache__ "$ROOT/scripts/" "$RUNTIME_OUT/scripts/"
  mkdir -p "$RUNTIME_OUT/docs/architecture"
  cp "$ROOT/docs/architecture/SQLITE_SCHEMA_V1.sql" "$RUNTIME_OUT/docs/architecture/"
fi

echo "==> Building $APP_NAME.app"
FLUTTER_BIN="$FLUTTER" BHE_RUNTIME_OUT="$RUNTIME_OUT" \
  BHE_SKIP_RUNTIME_PREPARE=1 BHE_SKIP_PACKAGE=1 \
  "$ROOT/scripts/build_macos_release.sh"

codesign --verify --deep --strict "$BUILT_APP"

if pgrep -xq "$APP_NAME"; then
  echo "==> Quitting running $APP_NAME"
  osascript -e "tell application id \"com.courtside.highlighteditor\" to quit" >/dev/null 2>&1 || true
  for _ in {1..20}; do
    pgrep -xq "$APP_NAME" || break
    sleep 0.5
  done
  if pgrep -xq "$APP_NAME"; then
    echo "$APP_NAME is still running; quit it and re-run this script." >&2
    exit 1
  fi
fi

echo "==> Installing to $TARGET_APP"
staging="$INSTALL_DIR/.$APP_NAME.app.installing"
rm -rf "$staging"
ditto "$BUILT_APP" "$staging"
rm -rf "$TARGET_APP"
mv "$staging" "$TARGET_APP"
xattr -dr com.apple.quarantine "$TARGET_APP" 2>/dev/null || true
codesign --verify --deep --strict "$TARGET_APP"

version="$(defaults read "$TARGET_APP/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null || echo '?')"
commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')"
echo "Installed $TARGET_APP (version $version, commit $commit)"
