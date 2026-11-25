#!/usr/bin/env bash
# Installiert stopguard auf dem Mac und richtet einen launchd-Job ein,
# der alle 30 Minuten 'stopguard run' ausführt (Benachrichtigung über die Mitteilungszentrale).
#
#   ./deploy/macos/install.sh            # installieren / aktualisieren
#   ./deploy/macos/install.sh --uninstall
set -euo pipefail

LABEL="de.stopguard.run"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_DIR="$HOME/.local/share/stopguard"
VENV="$APP_DIR/venv"
BIN_DIR="$HOME/.local/bin"
LOG="$HOME/Library/Logs/stopguard.log"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INTERVAL="${STOPGUARD_INTERVAL_SECONDS:-1800}"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST" "$BIN_DIR/stopguard"
  rm -rf "$VENV"
  echo "✔ stopguard deinstalliert (Konfiguration ~/.config/stopguard und Zustand ~/.local/state/stopguard bleiben erhalten)."
  exit 0
fi

# --- Python >= 3.11 finden -------------------------------------------------
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

# --- Scalable CLI prüfen ------------------------------------------------------
if ! command -v sc >/dev/null 2>&1; then
  echo "ⓘ Die offizielle Scalable CLI (sc) fehlt. Installation:"
  echo "    brew tap ScalableCapital/tap"
  echo "    brew trust --formula ScalableCapital/tap/scalable-cli"
  echo "    brew install scalable-cli"
  echo "  Danach im Scalable-Web: Profil → Sicherheit → Agentic Investing → CLI aktivieren."
fi
SC_DIR="$(dirname "$(command -v sc 2>/dev/null || echo /opt/homebrew/bin/sc)")"

# --- stopguard in eigenes venv installieren ---------------------------------------
mkdir -p "$APP_DIR" "$BIN_DIR" "$(dirname "$PLIST")" "$(dirname "$LOG")"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "$REPO_DIR"
ln -sf "$VENV/bin/stopguard" "$BIN_DIR/stopguard"
echo "✔ stopguard installiert: $BIN_DIR/stopguard"

if [[ ! -f "$HOME/.config/stopguard/config.toml" ]]; then
  "$VENV/bin/stopguard" init
  # macOS-Mitteilungen standardmäßig aktivieren
  sed -i '' 's/^channels = \["console"\]/channels = ["console", "macos"]/' "$HOME/.config/stopguard/config.toml"
fi

# --- launchd-Job ---------------------------------------------------------------
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/stopguard</string>
    <string>run</string>
    <string>--quiet</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$SC_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "✔ launchd-Job aktiv (alle $((INTERVAL / 60)) Min.). Log: $LOG"
echo
echo "Nächste Schritte:"
echo "  1. Konfiguration anpassen:   open -e ~/.config/stopguard/config.toml"
echo "  2. Bei Scalable anmelden:    stopguard login"
echo "  3. Prüfen:                   stopguard doctor && stopguard status"
echo "  4. Orders bestätigen:        stopguard apply   (oder Doppelklick auf deploy/macos/stopguard-apply.command)"
[[ ":$PATH:" == *":$BIN_DIR:"* ]] || echo "  ⓘ $BIN_DIR zum PATH hinzufügen, z. B.: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
