#!/usr/bin/env bash
# Clean-system install -> real use through ZeroClaw -> uninstall, as a brand-new user.
# Run as root (creates a disposable user). usage: run.sh /path/to/steward-src /path/to/zeroclaw
set -uo pipefail
SRC="$1"; ZC="$2"
U=stwfresh$$
FAILS=0
ok() { echo "PASS $1"; }
bad() { echo "FAIL $1"; FAILS=$((FAILS+1)); }

useradd -m "$U"
H=$(getent passwd "$U" | cut -d: -f6)
install -m 0755 "$ZC" /usr/local/bin/zeroclaw
cp -a "$SRC" "$H/steward-src"; chown -R "$U" "$H/steward-src"
as() { su "$U" -s /bin/bash -c "export PIP_CERT=${PIP_CERT:-} SSL_CERT_FILE=${PIP_CERT:-} REQUESTS_CA_BUNDLE=${PIP_CERT:-} CURL_CA_BUNDLE=${PIP_CERT:-} HTTPS_PROXY=${HTTPS_PROXY:-} https_proxy=${HTTPS_PROXY:-}; $*"; }

as "mkdir -p ~/.zeroclaw ~/Documents ~/Pictures ~/.ssh && echo 'thesis v3' > ~/Documents/thesis.docx \
  && echo photo > ~/Pictures/a.jpg && echo key > ~/.ssh/id_ed25519 && echo '# my rc' > ~/.bashrc"
cat > "$H/.zeroclaw/config.toml" <<'EOF'
schema_version = 3
# user's own comment
[providers.models.custom.fake]
uri = "http://127.0.0.1:28455/v1"
model = "fake"
api_key = "fake"
native_tools = true

[agents.assistant]
model_provider = "custom.fake"
EOF
chown -R "$U" "$H/.zeroclaw"
cp "$H/.zeroclaw/config.toml" /tmp/zc-orig-$U.toml
snap() { (cd "$H" && find . -type f ! -path './.cache/*' ! -path './.local/opt/*' ! -path './.local/share/steward/*' \
  ! -path './.config/steward/*' ! -path './steward-src/*' ! -path './.zeroclaw/*' ! -path './.local/bin/*' \
  ! -path './.fake/*' -print0 | sort -z | xargs -0 sha256sum); }
snap > /tmp/snap-before-$U

echo "== install as a new user"
if as "set -o pipefail; cd ~ && bash ~/steward-src/install.sh --yes --mode desktop 2>&1 | tail -25"; then ok "install.sh completed (incl. on-machine test suite)"; else bad "install.sh"; fi
as "test -x ~/.local/bin/steward && test -x ~/.local/opt/steward/.venv/bin/steward-server" && ok "commands installed" || bad "commands missing"
grep -q '# my own comment\|# user'"'"'s own comment' "$H/.zeroclaw/config.toml" && ok "user's ZeroClaw comments kept" || bad "comments lost"
grep -q '\[agents.steward\]' "$H/.zeroclaw/config.toml" && ok "ZeroClaw agent 'steward' added" || bad "agent not added"
ls "$H/.zeroclaw/" | grep -q 'config.toml.steward-backup' && ok "ZeroClaw config backed up before editing" || bad "no backup"

echo "== real use: ZeroClaw -> installed Steward"
cat > /tmp/use-$U.py <<EOF
import sys, json, subprocess, os
sys.path.insert(0, "$SRC/tests/zc_e2e")
import run_e2e as R
m = R.Model(R.scripted([("steward__plan", {"goal": "tidy", "steps": [{"do": "trash", "path": "~/Documents/thesis.docx"}],
                                           "verify": ["test ! -e ~/Documents/thesis.docx"]})]))
m.httpd.server_close()
EOF
cat > /tmp/use2-$U.py <<EOF
import sys, json, subprocess, os, threading
sys.path.insert(0, "$SRC/tests/zc_e2e")
import run_e2e as R
from http.server import ThreadingHTTPServer
script = R.scripted([("steward__plan", {"goal": "tidy", "steps": [{"do": "trash", "path": "~/Documents/thesis.docx"}],
                                        "verify": ["test ! -e ~/Documents/thesis.docx"]})])
m = R.Model(script)
m.httpd.shutdown(); m.httpd.server_close()
h = ThreadingHTTPServer(("127.0.0.1", 28455), m.httpd.RequestHandlerClass)
threading.Thread(target=h.serve_forever, daemon=True).start()
env = dict(os.environ, PATH="$SRC/tests/fakebin:" + os.environ["PATH"], FAKE_STATE=os.path.expanduser("~/.fake"),
           FAKE_ZENITY="approve", STEWARD_PASS_ENV="FAKE_STATE,FAKE_ZENITY", DISPLAY=":99")
os.makedirs(env["FAKE_STATE"], exist_ok=True)
p = subprocess.run(["zeroclaw", "agent", "-a", "steward", "-m", "tidy my documents"], env=env,
                   capture_output=True, text=True, timeout=180)
res = R.tool_results(m.requests[-1]["messages"])
print("RESULT:", res[0][:200].replace("\n", " | "))
sys.exit(0 if "VERIFIED" in res[0] else 1)
EOF
chmod a+r /tmp/use2-$U.py
if as "/usr/bin/python3 /tmp/use2-$U.py"; then ok "plan approved and verified through real ZeroClaw"; else bad "real use"; fi
[ ! -e "$H/Documents/thesis.docx" ] && ok "file moved to Steward's trash" || bad "file not trashed"
as "~/.local/bin/steward doctor | tail -25"

echo "== uninstall (default keeps records and trash)"
as "bash ~/steward-src/uninstall.sh"
[ ! -e "$H/.local/opt/steward" ] && [ ! -e "$H/.local/bin/steward" ] && ok "program files removed" || bad "program files left"
cmp -s "$H/.zeroclaw/config.toml" /tmp/zc-orig-$U.toml && ok "ZeroClaw config byte-identical to before install" || { bad "ZeroClaw config differs"; diff "$H/.zeroclaw/config.toml" /tmp/zc-orig-$U.toml; }
ls "$H"/.local/share/steward/trash/*/item >/dev/null 2>&1 && ok "trash (the user's file) kept" || bad "trash lost"
snap > /tmp/snap-after-$U
diff <(grep -v 'thesis.docx' /tmp/snap-before-$U) <(grep -v 'thesis.docx' /tmp/snap-after-$U) >/dev/null \
  && ok "no unrelated user file changed (sha256 of every file compared)" || { bad "unrelated files changed"; diff /tmp/snap-before-$U /tmp/snap-after-$U | head; }
OUT=$(as "bash ~/steward-src/uninstall.sh --purge-data"); echo "$OUT" | grep -q "KEPT" && [ -d "$H/.local/share/steward/trash" ] \
  && ok "--purge-data refuses to delete a non-empty trash" || bad "purge deleted trash"
as "bash ~/steward-src/uninstall.sh --purge-data --delete-trash-too" >/dev/null
[ ! -e "$H/.local/share/steward" ] && [ ! -e "$H/.config/steward" ] && ok "--delete-trash-too removes everything of Steward's" || bad "purge incomplete"

pkill -u "$U" 2>/dev/null; userdel -r "$U" 2>/dev/null; rm -rf /usr/local/bin/zeroclaw /tmp/*-$U* /tmp/pytest-of-$U
echo; [ $FAILS = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"
exit $FAILS
