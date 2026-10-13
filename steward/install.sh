#!/usr/bin/env bash
# Steward installer. Installs into its own folder and virtual environment.
# Touches only: ~/.local/opt/steward, ~/.local/bin/steward{,-server},
# ~/.config/steward/policy.json (created if missing) and, with your OK,
# a marked block in ZeroClaw's config.toml (backed up first).
#
#   bash install.sh [--mode desktop|chat|both|terminal] [--provider family.alias]
#                   [--zeroclaw-config PATH] [--no-zeroclaw] [--skip-tests] [--yes]
set -euo pipefail

MODE=""; PROVIDER=""; ZCFG=""; NO_ZC=0; SKIP_TESTS=0; YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2;;
    --provider) PROVIDER="$2"; shift 2;;
    --zeroclaw-config) ZCFG="$2"; shift 2;;
    --no-zeroclaw) NO_ZC=1; shift;;
    --skip-tests) SKIP_TESTS=1; shift;;
    --yes) YES=1; shift;;
    *) echo "unknown option $1"; exit 2;;
  esac
done

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/opt/steward"
VENV="$DEST/.venv"
CONF="$HOME/.config/steward"
ZCFG="${ZCFG:-${ZEROCLAW_CONFIG_DIR:+$ZEROCLAW_CONFIG_DIR/config.toml}}"
ZCFG="${ZCFG:-$HOME/.zeroclaw/config.toml}"

echo "== Steward install"
fail=0
need() {
  if command -v "$1" >/dev/null; then echo "   found    $1"
  else echo "   MISSING  $1  ($2)"; if [ "$3" = req ]; then fail=1; fi; fi
}
need python3 "sudo dnf install python3" req
need bwrap "sudo dnf install bubblewrap" req
need pkexec "admin steps need it: sudo dnf install polkit" opt
need zenity "desktop approvals need it: sudo dnf install zenity" opt
need zeroclaw "see https://github.com/zeroclaw-labs/zeroclaw" opt
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "   Python 3.11+ required"; fail=1; }
[ $fail = 0 ] || { echo "Install the missing requirements first. Nothing was changed."; exit 1; }

if [ -e "$DEST" ] && [ "$SRC" != "$DEST" ]; then
  BK="$DEST.previous-$(date +%Y%m%d-%H%M%S)"
  mv "$DEST" "$BK"; echo "   previous install kept at $BK"
fi
mkdir -p "$(dirname "$DEST")" "$HOME/.local/bin" "$CONF"
[ "$SRC" = "$DEST" ] || cp -a "$SRC" "$DEST"
rm -rf "$DEST/.venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "$DEST[test]"
ln -sf "$VENV/bin/steward" "$HOME/.local/bin/steward"
ln -sf "$VENV/bin/steward-server" "$HOME/.local/bin/steward-server"
# Short names 'ask' and 'explain', only if nothing else already uses them.
for short in ask explain; do
  target="$VENV/bin/steward-$short"
  existing="$(command -v "$short" || true)"
  if [ -z "$existing" ] || [ "$(readlink -f "$existing")" = "$(readlink -f "$target")" ]; then
    ln -sf "$target" "$HOME/.local/bin/$short"
  else
    echo "   '$short' is already taken by $existing; use: steward $short ..."
  fi
done

if [ ! -f "$CONF/policy.json" ]; then
  printf '{\n  "approval_mode": "%s",\n  "zeroclaw_config": "%s"\n}\n' "${MODE:-desktop}" "$ZCFG" > "$CONF/policy.json"
  echo "   created $CONF/policy.json"
fi

if [ $NO_ZC = 0 ]; then
  if [ -f "$ZCFG" ]; then
    ans=y
    if [ $YES = 0 ]; then
      read -r -p "Add Steward to ZeroClaw's config ($ZCFG)? A backup is made first. [y/N] " ans || ans=n
    fi
    if [ "${ans:-n}" = y ] || [ "${ans:-n}" = Y ]; then
      "$VENV/bin/steward" setup-zeroclaw --server "$VENV/bin/steward-server" \
        ${PROVIDER:+--provider "$PROVIDER"} ${MODE:+--mode "$MODE"} || \
        echo "   ZeroClaw setup did not complete (see above); you can rerun: steward setup-zeroclaw"
    fi
  else
    echo "   ZeroClaw config not found at $ZCFG; later run: steward setup-zeroclaw"
  fi
fi

echo; echo "== Health check"
"$VENV/bin/steward" doctor || true

if [ $SKIP_TESTS = 0 ]; then
  echo; echo "== Automated tests (throwaway home + fake system tools; your files are not touched)"
  ( cd "$DEST" && "$VENV/bin/python" -m pytest -q -p no:cacheprovider tests ) || {
    echo "Some tests FAILED on this machine. Do not rely on Steward until resolved."; exit 1; }
fi
echo
echo "Installed. Talk to agent 'steward' in ZeroClaw (e.g. zeroclaw agent -a steward)."
echo "Your controls: steward plans | steward show PLAN | steward stop PLAN | steward doctor"
