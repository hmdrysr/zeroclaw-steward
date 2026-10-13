#!/usr/bin/env bash
# One-step setup: ZeroClaw 0.8.5 (bundled, checksum-verified) + Steward, on Fedora x86-64.
#
#   bash setup.sh                 interactive (recommended)
#
# Options (for scripted use): --provider openrouter|xai|anthropic|openai|ollama|custom
#   --model ID  --uri URL (custom/ollama)  --daily USD  --monthly USD
#   --mode desktop|chat|both|terminal  --keep-config | --fresh-config
#   --no-model-test  --skip-tests
#
# Changes only: ~/.local/opt/zeroclaw-0.8.5, ~/.local/bin/{zeroclaw,zerocode},
# ~/.zeroclaw (created, or kept and extended; a fresh start moves the old one
# aside, never deletes it), and Steward's own folders (see steward/README.md).
set -euo pipefail

VER=0.8.5
TARBALL=zeroclaw-x86_64-unknown-linux-gnu.tar.gz
B="$(cd "$(dirname "$0")" && pwd)"
ZOPT="$HOME/.local/opt/zeroclaw-$VER"
BIN="$HOME/.local/bin"
ZHOME="${ZEROCLAW_CONFIG_DIR:-$HOME/.zeroclaw}"
ZCFG="$ZHOME/config.toml"
STAMP="$(date +%Y%m%d-%H%M%S)"

PROVIDER=""; MODEL=""; URI=""; DAILY=""; MONTHLY=""; MODE=""; CFGCHOICE=""; MODELTEST=ask; SKIP_TESTS=""; AIDES=ask
while [ $# -gt 0 ]; do
  case "$1" in
    --provider) PROVIDER="$2"; shift 2;;  --model) MODEL="$2"; shift 2;;
    --uri) URI="$2"; shift 2;;            --daily) DAILY="$2"; shift 2;;
    --monthly) MONTHLY="$2"; shift 2;;    --mode) MODE="$2"; shift 2;;
    --keep-config) CFGCHOICE=keep; shift;; --fresh-config) CFGCHOICE=fresh; shift;;
    --no-model-test) MODELTEST=no; shift;; --skip-tests) SKIP_TESTS=--skip-tests; shift;;
    --aides) AIDES=yes; shift;; --no-aides) AIDES=no; shift;;
    *) echo "unknown option $1"; exit 2;;
  esac
done

ask() {  # ask VAR "question" default
  local __v="${!1:-}"
  if [ -z "$__v" ]; then
    if [ -t 0 ]; then read -r -p "$2 [${3}]: " __v || true; fi
    __v="${__v:-$3}"
  fi
  printf -v "$1" '%s' "$__v"
}

echo "== 1/7 Checking this computer"
[ "$(uname -m)" = x86_64 ] || { echo "This bundle contains ZeroClaw for x86-64 only."; exit 1; }
fail=0
for c in python3 bwrap; do command -v $c >/dev/null && echo "   found    $c" || { echo "   MISSING  $c"; fail=1; }; done
for c in zenity pkexec; do command -v $c >/dev/null && echo "   found    $c" || echo "   missing  $c (recommended: sudo dnf install ${c/pkexec/polkit})"; done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "   Python 3.11+ required"; fail=1; }
[ $fail = 0 ] || { echo "Install: sudo dnf install python3 bubblewrap zenity polkit   (nothing was changed)"; exit 1; }

echo "== 2/7 Verifying and installing ZeroClaw $VER"
# The expected hash is pinned here (checked against the official release's SHA256SUMS
# when this installer was built), so a tampered download or mirror cannot pass.
PINNED_SHA="9dbb53af95132e3a934ab61b229fb7aec3565f1c9bf31851f1698be9160655f9"
URL="https://github.com/zeroclaw-labs/zeroclaw/releases/download/v$VER/$TARBALL"
if [ ! -f "$B/zeroclaw/$TARBALL" ]; then
  command -v curl >/dev/null || { echo "   curl is needed to download ZeroClaw (sudo dnf install curl)"; exit 1; }
  mkdir -p "$B/zeroclaw"
  echo "   downloading ZeroClaw $VER from its official GitHub release (~32 MB)..."
  curl -fSL --proto '=https' --tlsv1.2 -o "$B/zeroclaw/$TARBALL.part" "$URL" || {
    rm -f "$B/zeroclaw/$TARBALL.part"; echo "   download failed; check your internet connection. Nothing was installed."; exit 1; }
  mv "$B/zeroclaw/$TARBALL.part" "$B/zeroclaw/$TARBALL"
fi
want="$PINNED_SHA"
if [ -f "$B/zeroclaw/SHA256SUMS" ]; then
  listed="$(grep " $TARBALL\$" "$B/zeroclaw/SHA256SUMS" | awk '{print $1}')"
  [ "$listed" = "$PINNED_SHA" ] || { echo "   SHA256SUMS does not match the pinned hash; refusing."; exit 1; }
fi
have="$(sha256sum "$B/zeroclaw/$TARBALL" | awk '{print $1}')"
if [ -z "$want" ] || [ "$want" != "$have" ]; then
  rm -f "$B/zeroclaw/$TARBALL"
  echo "   CHECKSUM MISMATCH for $TARBALL - deleted it and refusing to install. (expected $want, got $have)"; exit 1
fi
echo "   checksum OK ($have, matches the release's SHA256SUMS)"
mkdir -p "$BIN" "$(dirname "$ZOPT")"
if [ -d "$ZOPT" ]; then mv "$ZOPT" "$ZOPT.previous-$STAMP"; fi
mkdir -p "$ZOPT"
tar -xzf "$B/zeroclaw/$TARBALL" -C "$ZOPT"
cp "$B/zeroclaw/LICENSE-MIT" "$B/zeroclaw/LICENSE-APACHE" "$B/zeroclaw/NOTICE" "$ZOPT/" 2>/dev/null || true
for exe in zeroclaw zerocode; do
  if [ -e "$BIN/$exe" ] && [ "$(readlink "$BIN/$exe" 2>/dev/null)" != "$ZOPT/$exe" ]; then
    if [ -L "$BIN/$exe" ] && [[ "$(readlink "$BIN/$exe")" == "$HOME/.local/opt/zeroclaw-"* ]]; then
      rm -f "$BIN/$exe"                    # an older copy installed by this bundle
    else
      mv "$BIN/$exe" "$BIN/$exe.previous-$STAMP"; echo "   your previous $exe kept as $BIN/$exe.previous-$STAMP"
    fi
  fi
  [ -x "$ZOPT/$exe" ] && ln -sf "$ZOPT/$exe" "$BIN/$exe"
done
ZC="$ZOPT/zeroclaw"
"$ZC" --version | sed 's/^/   installed /'
other="$(command -v zeroclaw || true)"
[ -n "$other" ] && [ "$(readlink -f "$other")" != "$(readlink -f "$ZC")" ] && \
  echo "   NOTE: '$other' comes first on your PATH; this setup always uses $ZC"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "   NOTE: add $BIN to your PATH (Fedora normally does this at login)";; esac

echo "== 3/7 ZeroClaw configuration"
if [ -f "$ZCFG" ]; then
  echo "   An existing ZeroClaw config was found at $ZCFG"
  ask CFGCHOICE "   keep it and add Steward to it, or start fresh (old one is moved aside, not deleted)? keep/fresh" keep
else
  CFGCHOICE=fresh
fi
if [ "$CFGCHOICE" = fresh ]; then
  if [ -e "$ZHOME" ]; then mv "$ZHOME" "$ZHOME.backup-$STAMP"; echo "   previous ZeroClaw folder moved to $ZHOME.backup-$STAMP"; fi
  echo "   Which AI provider should ZeroClaw use?"
  echo "     openrouter  many models, one key (https://openrouter.ai/keys)"
  echo "     xai         Grok directly (https://console.x.ai)"
  echo "     anthropic   Claude directly      openai   OpenAI directly"
  echo "     ollama      a local model (needs Ollama running; uses your own hardware)"
  echo "     custom      any OpenAI-compatible endpoint"
  ask PROVIDER "   provider" openrouter
  case "$PROVIDER" in
    openrouter) dm="anthropic/claude-sonnet-4";; xai) dm="grok-4";; ollama) dm="qwen2.5:7b";; *) dm="";;
  esac
  ask MODEL "   model id (as listed by your provider; it must support tool calling)" "$dm"
  [ -n "$MODEL" ] || { echo "   a model id is required"; exit 1; }
  extra=""
  case "$PROVIDER" in
    openrouter|xai|anthropic|openai) extra='live_pricing = true';;
    ollama) ask URI "   Ollama address" "http://127.0.0.1:11434"; extra="uri = \"$URI\"
native_tools = true";;
    custom) ask URI "   endpoint URL (ending in /v1)" ""; [ -n "$URI" ] || { echo "   URL required"; exit 1; }
            extra="uri = \"$URI\"
native_tools = true";;
    *) echo "   unknown provider '$PROVIDER'"; exit 1;;
  esac
  ask DAILY "   daily spending cap in USD (ZeroClaw blocks requests beyond it)" "2"
  ask MONTHLY "   monthly spending cap in USD" "20"
  mkdir -p "$ZHOME"; chmod 700 "$ZHOME"
  umask 077
  cat > "$ZCFG" <<EOF
schema_version = 3

[providers.models.$PROVIDER.main]
model = "$MODEL"
$extra

[cost]
enabled = true
daily_limit_usd = $DAILY
monthly_limit_usd = $MONTHLY

[cost.enforcement]
mode = "block"
EOF
  umask 022
  "$ZC" --config-dir "$ZHOME" config get "providers.models.$PROVIDER.main.model" >/dev/null 2>&1 || {
    echo "   ZeroClaw did not accept the new config:"; "$ZC" --config-dir "$ZHOME" config get "providers.models.$PROVIDER.main.model"; exit 1; }
  echo "   created $ZCFG (provider $PROVIDER.main, model $MODEL, caps \$$DAILY/day \$$MONTHLY/month, block)"
  if [ "$PROVIDER" != ollama ]; then
    if [ "$PROVIDER" = custom ]; then ask NEEDKEY "   does this endpoint need an API key? y/n" y; else NEEDKEY=y; fi
    if [ "$NEEDKEY" = y ]; then
      echo "   Paste your API key at ZeroClaw's masked prompt. ZeroClaw encrypts it; this script never sees it."
      "$ZC" --config-dir "$ZHOME" config set "providers.models.$PROVIDER.main.api_key" || {
        echo "   The key was not saved. Later run: zeroclaw config set providers.models.$PROVIDER.main.api_key"; }
    fi
  fi
  PROVREF="$PROVIDER.main"
else
  PROVREF=""
  [ -n "$PROVIDER" ] && PROVREF="$PROVIDER.main"
fi

echo "== 4/7 Installing Steward"
bash "$B/steward/install.sh" --yes ${MODE:+--mode "$MODE"} ${PROVREF:+--provider "$PROVREF"} \
     --zeroclaw-config "$ZCFG" $SKIP_TESTS

echo "== 5/7 Model check"
if [ "$MODELTEST" = ask ] && [ -t 0 ]; then
  read -r -p "   Send one short test message to the model now? It uses a few tokens. [y/N] " t || t=n
  [ "${t:-n}" = y ] && MODELTEST=yes || MODELTEST=no
fi
if [ "$MODELTEST" = yes ]; then
  if out="$("$ZC" --config-dir "$ZHOME" agent -a steward -m "Reply with the single word READY and nothing else." 2>&1)"; then
    echo "$out" | tail -3 | sed 's/^/   /'
  else
    echo "   The model did not answer:"; echo "$out" | grep -v '^ *[0-9]*: \|^ *at ' | tail -5 | sed 's/^/   /'
    echo "   Check the model id and key (zeroclaw config set providers.models.<provider>.main.api_key)."
  fi
else
  echo "   skipped"
fi

echo "== 6/7 Optional small local helper models"
if [ "${AIDES:-ask}" = ask ] && [ -t 0 ]; then
  echo "   Two tiny local models (about 135M and 500M parameters, ~0.6 GB download, ~0.7 GB RAM only"
  echo "   while answering) can help 'ask' pick between tasks and pick out names. They never decide"
  echo "   anything risky, and they are tested on this PC first: if they score too low they stay off."
  read -r -p "   Set them up now (needs Ollama, or llama.cpp's llama-server)? [y/N] " t || t=n
  [ "${t:-n}" = y ] && AIDES=yes || AIDES=no
fi
if [ "${AIDES:-no}" = yes ]; then
  if command -v ollama >/dev/null; then be=ollama; elif command -v llama-server >/dev/null; then be=llamacpp; else be=""; fi
  if [ -n "$be" ]; then
    "$HOME/.local/bin/steward" aides setup --backend "$be" || echo "   (aides not enabled; everything works without them)"
  else
    echo "   Neither Ollama nor llama-server is installed. Later: install one, then run: steward aides setup"
  fi
else
  echo "   skipped (later: steward aides setup)"
fi

echo "== 7/7 Done"
cat <<EOF
   Plain English, on this PC:  ask "how much space do I have?"     explain "sudo dnf remove -y foo"
   Chat with the AI:           zeroclaw agent -a steward
   Health:     steward doctor
   Records:    steward plans / steward show PLAN / steward stop PLAN
   Remove all: bash "$B/uninstall-all.sh"
EOF
