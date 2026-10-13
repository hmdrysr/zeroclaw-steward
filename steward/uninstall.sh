#!/usr/bin/env bash
# Removes Steward's program files and its ZeroClaw entries.
# KEEPS your records and Steward's trash (files you trashed through Steward)
# unless you pass --purge-data; even then, a non-empty trash is kept unless
# you also pass --delete-trash-too.
#
#   bash uninstall.sh [--purge-data] [--delete-trash-too]
set -euo pipefail
PURGE=0; TRASH_TOO=0
for a in "$@"; do
  case "$a" in --purge-data) PURGE=1;; --delete-trash-too) TRASH_TOO=1;; *) echo "unknown option $a"; exit 2;; esac
done
DEST="$HOME/.local/opt/steward"
DATA="$HOME/.local/share/steward"
CONF="$HOME/.config/steward"

echo "== Steward uninstall"
if [ -x "$DEST/.venv/bin/steward" ]; then
  "$DEST/.venv/bin/steward" remove-zeroclaw || echo "   could not edit ZeroClaw's config; remove the steward block by hand"
fi
for l in "$HOME/.local/bin/steward" "$HOME/.local/bin/steward-server" "$HOME/.local/bin/ask" "$HOME/.local/bin/explain"; do
  if [ -L "$l" ] && [[ "$(readlink "$l")" == "$DEST"/* ]]; then rm -f "$l"; echo "   removed $l"; fi
done
if [ -d "$DEST" ]; then rm -rf "$DEST"; echo "   removed $DEST"; fi

if [ $PURGE = 1 ]; then
  items=0
  [ -d "$DATA/trash" ] && items=$(find "$DATA/trash" -mindepth 1 -maxdepth 1 | wc -l)
  if [ "$items" -gt 0 ] && [ $TRASH_TOO = 0 ]; then
    echo "   KEPT $DATA: its trash still holds $items item(s) you may want back."
    echo "   Restore them, or rerun with --purge-data --delete-trash-too to delete permanently."
  else
    rm -rf "$DATA"; echo "   removed $DATA"
  fi
  rm -rf "$CONF"; echo "   removed $CONF"
else
  echo "   kept $DATA (records and trash) and $CONF (policy)"
fi
echo "Done."
