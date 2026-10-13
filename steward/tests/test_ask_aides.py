"""`ask` end to end (real engine, fake system tools) and the small-model aides
(fake local model server that misbehaves on purpose)."""
import hashlib
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import FAKE, HOME, ROOT
from steward.english import aides as aides_mod
from steward.english import models
from steward.english.aides import Aides
from steward.english.ask import Asker

OSR = ROOT / "tests" / "os-release"


def asker(eng, aides=None, consent=False):
    return Asker(eng, aides, consent=lambda q: consent)


def pkgs():
    return set(json.load(open(FAKE / "pkgs.json"))) if (FAKE / "pkgs.json").exists() else set()


# ================================================================ ask, no aides
def test_ask_read_only(eng):
    a = asker(eng).ask("how much memory am I using?")
    assert a.kind == "facts" and any(f.startswith("Memory:") for f in a.facts)


def test_ask_install_goes_through_approval(eng, monkeypatch):
    monkeypatch.setenv("FAKE_ZENITY", "decline")
    a = asker(eng).ask("please install ripgrep")
    assert a.kind == "not_done" and "Nothing was changed" in a.text and "ripgrep" not in pkgs()
    monkeypatch.setenv("FAKE_ZENITY", "approve")
    a = asker(eng).ask("please install ripgrep")
    assert a.kind == "done" and "checked" in a.text and "ripgrep" in pkgs()
    assert "To undo: steward undo r" in a.text


def test_ask_remove_checks_dependents_and_presence(eng):
    a = asker(eng).ask("remove libfoo")
    assert a.kind == "not_done" and "foo-app" in a.text and "libfoo" in pkgs()
    a = asker(eng).ask("uninstall notreallythere")
    assert a.kind == "not_done" and "not installed" in a.text


@pytest.mark.parametrize("osr", ["fedora", "ubuntu", "arch", "tumbleweed"])
def test_ask_update_each_distro(eng, monkeypatch, osr):
    monkeypatch.setenv("STEWARD_OS_RELEASE", str(OSR / osr))
    a = asker(eng).ask("update my system")
    assert a.kind == "done", a.text
    log = (FAKE / "pkgops.log").read_text()
    assert {"fedora": "dnf upgrade", "ubuntu": "apt-get upgrade", "arch": "pacman -Syu",
            "tumbleweed": "zypper dist-upgrade"}[osr] in log


def test_ask_service_resolution_and_suggestions(eng):
    a = asker(eng).ask("stop the bluetooth service")
    assert a.kind == "done" and "bluetooth" in a.text
    a = asker(eng).ask("restart the bluetoth service")
    assert a.kind == "not_done" and "bluetooth.service" in a.text      # suggestion, nothing done
    a = asker(eng).ask("is the cups service running")
    assert a.kind == "facts" and "cups.service (system service) is running" in a.text


def test_ask_stop_program(eng, tmp_path):
    exe = tmp_path / "stwdemoapp"
    shutil.copy("/bin/sleep", exe)
    p = subprocess.Popen([str(exe), "60"])
    try:
        a = asker(eng).ask("close stwdemoapp")
        p.wait(timeout=5)
        assert a.kind == "done" and p.returncode is not None
        a = asker(eng).ask("close stwdemoap")
        assert a.kind == "not_done" and "No program called" in a.text
    finally:
        p.kill()


def test_ask_empty_trash_purges_only_trash(eng):
    t = HOME / ".local/share/Trash"
    (t / "files").mkdir(parents=True)
    (t / "info").mkdir(parents=True)
    (t / "files/old.iso").write_bytes(b"x" * 10000)
    (t / "info/old.iso.trashinfo").write_text("[Trash Info]\n")
    a = asker(eng).ask("empty the trash")
    assert a.kind == "done", a.text
    assert not (t / "files").exists()
    assert not any((HOME / ".local/share/steward/trash").glob("*/item"))     # space really freed
    assert (HOME / "Documents/thesis.docx").exists()
    assert "PERMANENT" in (FAKE / "zenity.txt").read_text()


def test_ask_refuses_and_escalates_only_with_consent(eng):
    a = asker(eng, consent=False).ask("don't remove firefox")
    assert a.kind == "refused" and "won't do this" in a.text
    a = asker(eng, consent=False).ask("restart my computer")
    assert a.kind == "refused"
    import tempfile
    d = tempfile.mkdtemp(dir=str(HOME))
    (open(f"{d}/zeroclaw", "w")).write("#!/bin/sh\nexit 0\n")
    os.chmod(f"{d}/zeroclaw", 0o755)
    old = os.environ["PATH"]
    os.environ["PATH"] = d + ":" + old
    try:
        a = asker(eng, consent=True).ask("restart my computer")
        assert a.kind == "escalate"
    finally:
        os.environ["PATH"] = old


def test_ask_questions_get_facts_not_actions(eng):
    a = asker(eng).ask("should I remove cowsay?")
    assert a.kind == "refused" and not eng.store.q("SELECT 1 FROM runs WHERE kind='step'")


# ================================================================ fake local model
class FakeModel:
    """Answers per role with whatever the test scripts, and records requests."""

    def __init__(self):
        self.replies = {}
        self.requests = []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                me.requests.append(body)
                sysmsg = body["messages"][0]["content"]
                role = "router" if "task id" in sysmsg else "extract" if "Copy the" in sysmsg else "select"
                r = me.replies.get(role, "{}")
                if r == "HANG":
                    import time
                    time.sleep(5)
                    r = "{}"
                data = json.dumps({"choices": [{"message": {"content": r if isinstance(r, str) else json.dumps(r)}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def aides(self, **kw):
        return Aides({"enabled": True, "backend": "ollama", "endpoint": self.url, "router_model": "r",
                      "helper_model": "h", "timeout_secs": 2} | kw)

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def fm():
    m = FakeModel()
    yield m
    m.close()


def test_requests_are_schema_constrained_and_deterministic(fm):
    fm.replies["router"] = {"id": "memory"}
    a = fm.aides()
    assert a.route("ram?", [("memory", "x"), ("disk", "y")]) == "memory"
    req = fm.requests[-1]
    assert req["temperature"] == 0
    schema = req["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["id"]["enum"] == ["memory", "disk", "none"]


@pytest.mark.parametrize("reply", [{"id": "remove_everything"}, {"id": 3}, "not json", {"nope": 1}, "[1,2]", "HANG"])
def test_router_garbage_is_ignored(fm, reply):
    fm.replies["router"] = reply
    assert fm.aides().route("x", [("memory", "x"), ("disk", "y")]) is None


def test_router_cannot_start_a_change(eng, fm, monkeypatch):
    from steward.english import intents
    monkeypatch.setattr(intents, "match", lambda t: intents.Match("ambiguous", candidates=["remove", "disk"]))
    import steward.english.ask as askmod
    monkeypatch.setattr(askmod.intents, "match", intents.match)
    fm.replies["router"] = {"id": "remove"}
    a = Asker(eng, fm.aides(), consent=lambda q: False).ask("whatever")
    assert a.kind == "question" and "Did you mean" in a.text
    assert not eng.store.q("SELECT 1 FROM runs WHERE kind='step'")


def test_extractor_must_copy_word_for_word(fm):
    fm.replies["extract"] = {"name": "gimp"}
    assert fm.aides().extract_name("put the gimp editor on here", "package") == "gimp"
    fm.replies["extract"] = {"name": "firefox"}           # hallucinated: not in the request
    assert fm.aides().extract_name("put the gimp editor on here", "package") is None
    fm.replies["extract"] = {"name": "gimp; rm -rf ~"}
    assert fm.aides().extract_name("gimp; rm -rf ~", "package") is None


def test_selector_cannot_drop_warnings_or_invent(fm):
    facts = ["Memory: 1 GB of 8 GB in use.", "Swap: none.", "Disk /: 110 GB used of 118 GB (97%), 1 GB free. It is FULL.",
             "Biggest memory users: firefox 1 GB.", "Failed system services: none."]
    fm.replies["select"] = {"ids": [0]}
    keep = fm.aides().select_facts("how much memory", facts)
    assert keep == [0, 2]                                 # the FULL disk warning is always kept
    for bad in ({"ids": [9]}, {"ids": [0, 0]}, {"ids": "0"}, {"ids": []}, "garbage"):
        fm.replies["select"] = bad
        assert fm.aides().select_facts("q", facts) is None


def test_aides_refuse_non_local_endpoints():
    a = Aides({"enabled": True, "backend": "ollama", "endpoint": "https://api.example.com/v1",
               "router_model": "r", "helper_model": "h", "timeout_secs": 2})
    assert a.route("x", [("memory", "x"), ("disk", "y")]) is None
    assert any("only use a local endpoint" in w for _, w in a.log)


def test_ask_with_selector_shows_facts_verbatim(eng, fm):
    fm.replies["select"] = {"ids": [1]}
    a = Asker(eng, fm.aides(), consent=lambda q: False).ask("why is my computer slow")
    shown = [l[2:] for l in a.text.splitlines() if l.startswith("• ")]
    assert shown and all(s in a.facts for s in shown)               # nothing rewritten
    assert "more detail" in a.text


# ================================================================ exam gate and download integrity
def _exam_replies(fm, good):
    def answer_router(q):
        for text, opts, right in models.EXAM["router"]:
            if text in q:
                return sorted(right)[0] if good else "none"
        return "none"
    orig = fm.httpd.RequestHandlerClass.do_POST

    def do_POST(h):
        body = json.loads(h.rfile.read(int(h.headers["Content-Length"])))
        sysmsg, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if "task id" in sysmsg:
            r = {"id": answer_router(user)}
        elif "Copy the" in sysmsg:
            name = ""
            for text, kind, right in models.EXAM["extractor"]:
                if text == user:
                    name = sorted(x or "" for x in right)[-1] if good else "wrong"
            r = {"name": name}
        else:
            r = {"ids": [1] if "memory" in user.split("\n")[0] else [2]} if good else {"ids": [0]}
        data = json.dumps({"choices": [{"message": {"content": json.dumps(r)}}]}).encode()
        h.send_response(200)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
    fm.httpd.RequestHandlerClass.do_POST = do_POST


@pytest.mark.parametrize("good", [True, False])
def test_exam_enables_only_when_models_pass(fm, good):
    _exam_replies(fm, good)
    cfg = {"enabled": False, "backend": "ollama", "endpoint": fm.url, "router_model": "r",
           "helper_model": "h", "timeout_secs": 2}
    passed, scores = models.exam(cfg, say=lambda *a: None)
    assert passed is good
    assert aides_mod.load()["enabled"] is good


def test_download_checksum_mismatch_deletes_file(monkeypatch, tmp_path):
    payload = b"GGUF-fake-model"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if "/api/models/" in self.path:
                data = json.dumps([{"path": "m-q8_0.gguf", "lfs": {"oid": "0" * 64, "size": len(payload)}}]).encode()
            else:
                data = payload
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(models, "_get", lambda url, timeout=60: __import__("urllib.request").request.urlopen(
        url.replace("https://huggingface.co", base), timeout=timeout))
    with pytest.raises(RuntimeError, match="CHECKSUM MISMATCH"):
        models.hf_download("router", say=lambda *a: None)
    assert not list(models.MODELS_DIR().glob("*.gguf*"))
    good = hashlib.sha256(payload).hexdigest()
    H.do_GET_orig = H.do_GET

    def ok_get(self):
        if "/api/models/" in self.path:
            data = json.dumps([{"path": "m-q8_0.gguf", "lfs": {"oid": good, "size": len(payload)}}]).encode()
        else:
            data = payload
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    H.do_GET = ok_get
    path = models.hf_download("router", say=lambda *a: None)
    assert open(path, "rb").read() == payload
    srv.shutdown()
