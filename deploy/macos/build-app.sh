#!/usr/bin/env bash
# Baut die macOS-App „stopguard.app“ (natives Fenster um die Weboberfläche).
#
#   ./deploy/macos/build-app.sh               # stopguard installieren/aktualisieren, App bauen, nach ~/Applications kopieren
#   ./deploy/macos/build-app.sh --app-only    # nur die App bauen (stopguard ist schon installiert, z. B. per install.sh)
#   ./deploy/macos/build-app.sh --universal   # Apple Silicon + Intel in einer App
#   ./deploy/macos/build-app.sh --no-install  # nicht nach ~/Applications kopieren (Ergebnis: dist/stopguard.app)
#
# Voraussetzungen: macOS 12+, Xcode Command Line Tools (xcode-select --install), Python ≥ 3.11.
# Die App enthält kein Python: Sie startet das installierte stopguard aus ~/.local/share/stopguard/venv
# (derselbe Ort wie bei install.sh). Der launchd-Job von install.sh ist unabhängig davon und optional.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$REPO_DIR/deploy/macos/app"
OUT="${STOPGUARD_APP_OUT:-$REPO_DIR/dist}"
APP="$OUT/stopguard.app"
VENV="$HOME/.local/share/stopguard/venv"
BIN_DIR="$HOME/.local/bin"
APP_ONLY=0
UNIVERSAL=0
INSTALL=1
for arg in "$@"; do
  case "$arg" in
    --app-only) APP_ONLY=1 ;;
    --universal) UNIVERSAL=1 ;;
    --no-install) INSTALL=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$(uname)" != "Darwin" ]]; then
  echo "✖ Die macOS-App lässt sich nur auf einem Mac bauen." >&2
  exit 1
fi
if ! command -v swiftc >/dev/null 2>&1; then
  echo "✖ swiftc fehlt. Installiere die Xcode Command Line Tools: xcode-select --install" >&2
  exit 1
fi

# --- 1) stopguard installieren / aktualisieren ---------------------------------
if [[ $APP_ONLY -eq 0 ]]; then
  PY=""
  for cand in python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      PY="$(command -v "$cand")"; break
    fi
  done
  if [[ -z "$PY" ]]; then
    echo "✖ Python >= 3.11 nicht gefunden. Installation z. B. mit: brew install python@3.13" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$VENV")" "$BIN_DIR"
  [[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet --upgrade "$REPO_DIR"
  ln -sf "$VENV/bin/stopguard" "$BIN_DIR/stopguard"
  echo "✔ stopguard installiert: $VENV/bin/stopguard"
fi

# --- 2) App-Bundle -------------------------------------------------------------
VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$REPO_DIR/pyproject.toml" | head -1)"
BUILD="$(git -C "$REPO_DIR" rev-list --count HEAD 2>/dev/null || echo 1)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
sed -e "s/__VERSION__/${VERSION:-0.0.0}/" -e "s/__BUILD__/${BUILD}/" "$SRC/Info.plist" > "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null

SWIFT_FLAGS=(-O -swift-version 5 -framework AppKit -framework WebKit -framework Security)
if [[ $UNIVERSAL -eq 1 ]]; then
  TMP="$(mktemp -d)"
  swiftc "${SWIFT_FLAGS[@]}" -target arm64-apple-macos12 -o "$TMP/stopguard-arm64" "$SRC/main.swift"
  swiftc "${SWIFT_FLAGS[@]}" -target x86_64-apple-macos12 -o "$TMP/stopguard-x86_64" "$SRC/main.swift"
  lipo -create "$TMP/stopguard-arm64" "$TMP/stopguard-x86_64" -output "$APP/Contents/MacOS/stopguard"
  rm -rf "$TMP"
else
  swiftc "${SWIFT_FLAGS[@]}" -target "$(uname -m)-apple-macos12" -o "$APP/Contents/MacOS/stopguard" "$SRC/main.swift"
fi
echo "✔ App kompiliert ($(lipo -archs "$APP/Contents/MacOS/stopguard"))"

# --- 3) Icon -------------------------------------------------------------------
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z $size $size "$SRC/AppIcon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) "$SRC/AppIcon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"

# --- 4) Signatur (ad hoc, für den eigenen Mac) ---------------------------------
codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1 || echo "ⓘ Ad-hoc-Signatur nicht möglich – die App läuft trotzdem lokal."
echo "✔ Gebaut: $APP"

# --- 5) Installieren -------------------------------------------------------------
if [[ $INSTALL -eq 1 ]]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/stopguard.app"
  cp -R "$APP" "$HOME/Applications/"
  echo "✔ Installiert: ~/Applications/stopguard.app – im Finder öffnen oder: open ~/Applications/stopguard.app"
fi
if [[ ! -f "${XDG_CONFIG_HOME:-$HOME/.config}/stopguard/config.toml" ]]; then
  echo "ⓘ Noch keine Konfiguration: Die App bietet beim ersten Start das Demo-Depot oder die Einrichtung im Terminal an."
fi
