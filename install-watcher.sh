#!/bin/bash
# Install (or remove) the LaunchAgent that watches for the phone being plugged in.
#
#   ./install-watcher.sh            install and start
#   ./install-watcher.sh uninstall  stop and remove
#   ./install-watcher.sh status     is it running?

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="fr.bellenguez.easytransfer"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
PYTHON="$(command -v python3)"

case "${1:-install}" in

uninstall)
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✅ EasyTransfer watcher removed."
  ;;

status)
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "✅ Watcher is loaded."
    launchctl print "$DOMAIN/$LABEL" | grep -E '^\s+(state|pid) ' || true
  else
    echo "❌ Watcher is not loaded."
  fi
  ;;

install)
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.cache/easytransfer"

  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-u</string>
    <string>$HERE/watcher.py</string>
  </array>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>ProcessType</key>      <string>Background</string>
  <key>StandardOutPath</key>  <string>$HOME/.cache/easytransfer/watcher.out</string>
  <key>StandardErrorPath</key><string>$HOME/.cache/easytransfer/watcher.out</string>
</dict>
</plist>
PLISTEOF

  # Reload cleanly if it was already installed.
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"

  echo "✅ EasyTransfer watcher installed and running."
  echo "   Plug the phone in — a dialog will offer to open the app."
  echo "   Remove it any time with: $HERE/install-watcher.sh uninstall"
  ;;

*)
  echo "usage: $0 [install|uninstall|status]" >&2
  exit 1
  ;;
esac
