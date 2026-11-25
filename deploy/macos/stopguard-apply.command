#!/bin/zsh
# Doppelklick im Finder öffnet das Terminal: Status anzeigen und Order-Änderungen bestätigen.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
clear
stopguard apply
echo
read -k 1 "?Fertig – beliebige Taste zum Schließen …"
