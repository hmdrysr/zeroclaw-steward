#!/usr/bin/env bash
# Builds dist/steward-setup.sh, the one-file installer, from this repository.
#
#   bash tools/build-installer.sh            build it
#   bash tools/build-installer.sh --check    fail if dist/ is out of date (used by CI)
#
# The build is reproducible: same source files give the same installer,
# byte for byte (sorted names, fixed owner, modes and time, gzip -n).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' steward/pyproject.toml)"
STAMP="@1767225600"   # 2026-01-01 00:00 UTC, in the past so tar never warns
OUT="dist/steward-setup.sh"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/zeroclaw-steward"
# Only what the installer needs; never the ZeroClaw binary (setup.sh downloads
# it from the official release and checks it against a pinned SHA-256).
for p in README.md setup.sh uninstall-all.sh steward tests zeroclaw; do
  cp -a "$p" "$TMP/zeroclaw-steward/"
done
find "$TMP" \( -name '__pycache__' -o -name '.pytest_cache' \) -prune -exec rm -rf {} +
find "$TMP" \( -name '*.pyc' -o -name '*.tar.gz' -o -name '*.part' \) -delete

tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="$STAMP" \
    --mode='u+rwX,go+rX,go-w' -C "$TMP" -cf - zeroclaw-steward | gzip -n -9 > "$TMP/p.tgz"
SHA="$(sha256sum "$TMP/p.tgz" | cut -d' ' -f1)"

{
cat <<EOF
#!/usr/bin/env bash
# ZeroClaw + Steward installer (self-contained). Version $VERSION.
#
# What this file does, in order:
#   1. Checks its own embedded package against the SHA-256 written below.
#   2. Unpacks it to ~/.local/src/zeroclaw-steward-$VERSION (an older copy there is moved aside).
#   3. Runs setup.sh, which asks you questions, downloads ZeroClaw 0.8.5 from its official
#      GitHub release and refuses it unless it matches a pinned SHA-256.
# Nothing is installed system-wide and nothing runs as root.
# Options are passed to setup.sh (e.g. --no-aides). Read it: everything above the
# __PAYLOAD__ line is the whole program; below it is the package, base64-encoded.
# Built from source by tools/build-installer.sh.
set -euo pipefail
PAYLOAD_SHA="$SHA"
VERSION="$VERSION"
EOF
cat <<'EOF'
DEST="$HOME/.local/src/zeroclaw-steward-$VERSION"
if [ ! -t 0 ]; then
  echo "Please run this file from a terminal (bash steward-setup.sh), not through a pipe: it asks you questions." >&2
  exit 1
fi
SELF="${BASH_SOURCE[0]}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
line=$(grep -an '^__PAYLOAD__$' "$SELF" | tail -1 | cut -d: -f1)
if [ -z "$line" ] || ! tail -n +"$((line + 1))" "$SELF" | base64 -d > "$TMP/p.tgz" 2>/dev/null; then
  have="unreadable"
else
  have=$(sha256sum "$TMP/p.tgz" | cut -d' ' -f1)
fi
if [ "$have" != "$PAYLOAD_SHA" ]; then
  echo "This installer is damaged or was changed (package checksum mismatch). Nothing was installed." >&2
  exit 1
fi
echo "Package checksum OK ($have)."
mkdir -p "$HOME/.local/src"
if [ -e "$DEST" ]; then mv "$DEST" "$DEST.previous-$(date +%Y%m%d-%H%M%S)"; fi
mkdir -p "$DEST"
tar -xzf "$TMP/p.tgz" -C "$DEST" --strip-components=1
echo "Unpacked to $DEST"
exec bash "$DEST/setup.sh" "$@"
__PAYLOAD__
EOF
base64 -w 76 "$TMP/p.tgz"
} > "$TMP/steward-setup.sh"

if [ $CHECK = 1 ]; then
  if cmp -s "$TMP/steward-setup.sh" "$OUT"; then echo "dist/ is up to date."; exit 0; fi
  echo "dist/steward-setup.sh is out of date. Run: bash tools/build-installer.sh, and commit dist/." >&2
  exit 1
fi
mkdir -p dist
install -m 0755 "$TMP/steward-setup.sh" "$OUT"
( cd dist && sha256sum steward-setup.sh > steward-setup.sh.sha256 )
echo "Built $OUT"
cat dist/steward-setup.sh.sha256
