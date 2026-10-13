#!/usr/bin/env bash
# Removes Steward and the bundled ZeroClaw program.
# Keeps by default: your ZeroClaw folder (~/.zeroclaw: config, encrypted API key,
# memory) and Steward's records and trash.
#
#   bash uninstall-all.sh [--remove-zeroclaw-config] [--purge-steward-data] [--delete-trash-too]
#
# --remove-zeroclaw-config moves ~/.zeroclaw to ~/.zeroclaw.removed-DATE (not deleted).
set -euo pipefail
VER=0.8.5
ZOPT="$HOME/.local/opt/zeroclaw-$VER"
BIN="$HOME/.local/bin"
ZHOME="${ZEROCLAW_CONFIG_DIR:-$HOME/.zeroclaw}"
RMCFG=0; SARGS=()
for a in "$@"; do
  case "$a" in
    --remove-zeroclaw-config) RMCFG=1;;
    --purge-steward-data) SARGS+=(--purge-data);;
    --delete-trash-too) SARGS+=(--delete-trash-too);;
    *) echo "unknown option $a"; exit 2;;
  esac
done

if [ -f "$HOME/.local/opt/steward/uninstall.sh" ]; then
  bash "$HOME/.local/opt/steward/uninstall.sh" "${SARGS[@]}"
fi
echo "== ZeroClaw program"
for exe in zeroclaw zerocode; do
  if [ -L "$BIN/$exe" ] && [ "$(readlink "$BIN/$exe")" = "$ZOPT/$exe" ]; then
    rm -f "$BIN/$exe"; echo "   removed $BIN/$exe"
    prev="$(ls -1d "$BIN/$exe.previous-"* 2>/dev/null | tail -1 || true)"
    if [ -n "$prev" ]; then mv "$prev" "$BIN/$exe"; echo "   restored your earlier $exe"; fi
  fi
done
[ -d "$ZOPT" ] && rm -rf "$ZOPT" && echo "   removed $ZOPT"
if [ $RMCFG = 1 ] && [ -d "$ZHOME" ]; then
  mv "$ZHOME" "$ZHOME.removed-$(date +%Y%m%d-%H%M%S)"; echo "   moved $ZHOME aside (delete it yourself when sure)"
else
  [ -d "$ZHOME" ] && echo "   kept $ZHOME (config, encrypted key, memory)"
fi
echo "Done."
