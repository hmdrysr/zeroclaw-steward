#!/usr/bin/env bash
# Fresh-machine test of the whole bundle as a brand-new user. Run as root.
# usage: fresh_setup.sh /path/to/bundle
set -uo pipefail
B="$1"; INST="${2:-}"; U=zcfresh$$; FAILS=0
ok() { echo "PASS $1"; }; bad() { echo "FAIL $1"; FAILS=$((FAILS+1)); }
useradd -m "$U"; H=$(getent passwd "$U" | cut -d: -f6)
cp -a "$B" "$H/bundle"; chown -R "$U" "$H/bundle"
if [ -n "$INST" ]; then cp "$INST" "$H/steward-setup.sh"; chown "$U" "$H/steward-setup.sh"; RUN='bash ~/steward-setup.sh'; else RUN='bash ~/bundle/setup.sh'; fi
as() { su "$U" -s /bin/bash -c "export PIP_CERT=${PIP_CERT:-} CURL_CA_BUNDLE=${PIP_CERT:-} SSL_CERT_FILE=${PIP_CERT:-} REQUESTS_CA_BUNDLE=${PIP_CERT:-} HTTPS_PROXY=${HTTPS_PROXY:-} https_proxy=${HTTPS_PROXY:-} PATH=\$HOME/.local/bin:\$HOME/bundle/steward/tests/fakebin:/usr/local/bin:/usr/bin:/bin FAKE_STATE=\$HOME/.fake FAKE_ZENITY=approve STEWARD_PASS_ENV=FAKE_STATE,FAKE_ZENITY DISPLAY=:99; mkdir -p \$HOME/.fake; $*"; }
as "mkdir -p ~/Documents ~/Pictures && echo 'thesis' > ~/Documents/thesis.docx && echo pic > ~/Pictures/a.jpg"
snap() { (cd "$H" && find . -type f ! -path './.cache/*' ! -path './.local/*' ! -path './.config/*' ! -path './.zeroclaw/*' \
  ! -path './bundle/*' ! -path './.fake/*' ! -path './.zeroclaw.*' -print0 | sort -z | xargs -0 sha256sum); }
snap > /tmp/fs-before-$U

# fake model on a fixed port, answering READY and logging auth headers
cat > /tmp/fm-$U.py <<PY
import json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, "$H/bundle/steward/tests/zc_e2e")
import run_e2e as R
plan = {"goal": "tidy", "steps": [{"do": "trash", "path": "~/Documents/thesis.docx"}], "verify": ["test ! -e ~/Documents/thesis.docx"]}
calls = {"n": 0}
def script(messages, n):
    users = [m for m in messages if m.get("role") == "user"]
    last = str(users[-1].get("content")) if users else ""
    if "READY" in last: return ("text", "READY")
    res = R.tool_results(messages)
    if not res: return ("tool", "steward__plan", plan)
    return ("text", "E2E-DONE " + res[-1][:200].replace("\n", " | "))
m = R.Model(script); m.httpd.shutdown(); m.httpd.server_close()
h = ThreadingHTTPServer(("127.0.0.1", 28466), m.httpd.RequestHandlerClass)
def dump():
    import time
    while True:
        time.sleep(1); open("/tmp/fm-auth-$U", "w").write(json.dumps(m.auth))
threading.Thread(target=dump, daemon=True).start()
h.serve_forever()
PY
python3 /tmp/fm-$U.py & FM=$!; sleep 1

echo "== setup.sh (interactive answers through a pseudo-terminal)"
as "python3 ~/bundle/tests/pty_answer.py /tmp/setup-$U.log \
  'provider \\[=>custom' 'model id.*\\[=>fake' 'endpoint URL=>http://127.0.0.1:28466/v1' 'need an API key=>y' \
  'daily spending cap.*\\[=>1' 'monthly spending cap.*\\[=>5' 'Enter value for=>sk-test-SECRETKEY-12345' \
  'test message.*\\[y/N\\]=>y' 'Set them up now.*\\[y/N\\]=>y' -- $RUN"
rc=$?
tail -40 /tmp/setup-$U.log | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g'
[ $rc = 0 ] && ok "setup.sh finished" || bad "setup.sh exit $rc"
grep -q "checksum OK" /tmp/setup-$U.log && ok "ZeroClaw checksum verified before install" || bad "no checksum line"
[ -n "$INST" ] && { grep -q "Package checksum OK" /tmp/setup-$U.log && grep -q "downloading ZeroClaw" /tmp/setup-$U.log && ok "installer checked itself, downloaded ZeroClaw from GitHub" || bad "installer self-check/download"; }
[ -L "$H/.local/bin/zeroclaw" ] && as "zeroclaw --version" | grep -q "0.8.5" && ok "ZeroClaw 0.8.5 installed" || bad "zeroclaw missing"
[ "$(stat -c %a "$H/.zeroclaw/config.toml")" = 600 ] && ok "config.toml readable only by the user" || bad "config perms $(stat -c %a "$H/.zeroclaw/config.toml")"
grep -q 'api_key = "enc2:' "$H/.zeroclaw/config.toml" && ok "API key stored encrypted" || bad "key not encrypted"
found=$(grep -rlIs "sk-test-SECRETKEY-12345" "$H" --exclude-dir=bundle | grep -v '/tests/fresh_setup.sh$' || true)
[ -n "$found" ] && { bad "PLAINTEXT KEY FOUND in home: $found"; } || ok "plaintext key not found anywhere in home (besides this test script itself)"
sleep 2; grep -q "Bearer sk-test-SECRETKEY-12345" /tmp/fm-auth-$U && ok "ZeroClaw decrypted the key and sent it only to the model endpoint" || bad "auth header missing"
grep -q 'mode = "block"' "$H/.zeroclaw/config.toml" && grep -q "daily_limit_usd = 1" "$H/.zeroclaw/config.toml" && ok "spending caps set to block" || bad "cost caps"
grep -q "READY" /tmp/setup-$U.log && ok "model test message answered" || bad "model test"
grep -q "Neither Ollama nor llama-server" /tmp/setup-$U.log && ok "aides: honest message when no local model runtime" || bad "aides message"
as "steward doctor" > /tmp/doc-$U.log 2>&1; grep -q "ZeroClaw cannot bypass Steward" /tmp/doc-$U.log && ok "doctor: ZeroClaw config gate passes" || { bad "doctor gate"; grep -i "zeroclaw" /tmp/doc-$U.log; }
grep -q "hosted\|local=True" /tmp/doc-$U.log && ok "doctor reports which model/provider is used" || bad "doctor provider info"

echo "== use it"
as "zeroclaw agent -a steward -m 'tidy my documents'" > /tmp/use-$U.log 2>&1
grep -q "E2E-DONE plan .* VERIFIED" /tmp/use-$U.log && [ ! -e "$H/Documents/thesis.docx" ] && ok "AI plan through ZeroClaw -> Steward: VERIFIED" || { bad "AI plan"; tail -5 /tmp/use-$U.log; }
as "ask how much memory am I using" | grep -q "Memory:" && ok "ask answers in plain English" || bad "ask"
as "explain 'sudo rm -rf /'" | grep -q "CRITICAL" && ok "explain flags a destructive command" || bad "explain"

echo "== uninstall-all"
if [ -n "$INST" ]; then UN='bash ~/.local/src/zeroclaw-steward-2.0.0/uninstall-all.sh'; else UN='bash ~/bundle/uninstall-all.sh'; fi
as "$UN" > /tmp/un-$U.log 2>&1; cat /tmp/un-$U.log
[ ! -e "$H/.local/opt/zeroclaw-0.8.5" ] && [ ! -e "$H/.local/bin/zeroclaw" ] && [ ! -e "$H/.local/opt/steward" ] && ok "programs removed" || bad "programs left"
[ -f "$H/.zeroclaw/config.toml" ] && ok "ZeroClaw config + encrypted key kept by default" || bad "zeroclaw config removed"
! grep -q "steward" "$H/.zeroclaw/config.toml" && ok "Steward's entries removed from ZeroClaw config" || bad "steward entries left"
snap > /tmp/fs-after-$U
diff <(grep -v thesis.docx /tmp/fs-before-$U) <(grep -v thesis.docx /tmp/fs-after-$U) >/dev/null && ok "no unrelated user file changed" || { bad "files changed"; diff /tmp/fs-before-$U /tmp/fs-after-$U | head; }

if [ -n "$INST" ]; then
  echo "== changed installer is refused"
  cp "$INST" /tmp/tinst-$U.sh; sed -i '$ s/^\(.\)/\1A/' /tmp/tinst-$U.sh; python3 - /tmp/tinst-$U.sh <<'PY'
import sys
p = sys.argv[1]; b = bytearray(open(p, "rb").read()); i = b.rindex(b"\n__PAYLOAD__\n") + 500
b[i] = ord("A") if b[i] != ord("A") else ord("B"); open(p, "wb").write(b)
PY
  chown "$U" /tmp/tinst-$U.sh
  as "python3 ~/bundle/tests/pty_answer.py /tmp/tl-$U.log -- bash /tmp/tinst-$U.sh"
  grep -q "damaged or was changed" /tmp/tl-$U.log && [ ! -e "$H/.local/opt/zeroclaw-0.8.5" ] && ok "changed installer refused, nothing installed" || { bad "changed installer"; tail -3 /tmp/tl-$U.log; }
  as "bash ~/steward-setup.sh < /dev/null" > /tmp/np-$U.log 2>&1
  grep -q "from a terminal" /tmp/np-$U.log && ok "refuses to run through a pipe (it must ask you questions)" || bad "pipe guard"
fi
echo "== tampered download is refused"
cp -a "$B" /tmp/tamper-$U; printf 'x' >> /tmp/tamper-$U/zeroclaw/zeroclaw-x86_64-unknown-linux-gnu.tar.gz; chown -R "$U" /tmp/tamper-$U
as "bash /tmp/tamper-$U/setup.sh --keep-config --no-model-test --skip-tests --no-aides" > /tmp/tm-$U.log 2>&1
grep -q "CHECKSUM MISMATCH" /tmp/tm-$U.log && [ ! -e "$H/.local/opt/zeroclaw-0.8.5" ] && ok "tampered ZeroClaw archive refused, nothing installed" || { bad "tamper"; tail -3 /tmp/tm-$U.log; }

kill $FM 2>/dev/null; pkill -u "$U" 2>/dev/null; sleep 1; userdel -r "$U" 2>/dev/null
rm -rf /tmp/*-$U* /tmp/tamper-$U /tmp/pytest-of-$U
echo; [ $FAILS = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"; exit $FAILS
