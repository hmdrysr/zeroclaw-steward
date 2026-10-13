"""Optional small local models as aides. They never decide anything risky.

router     (~0.1B, SmolLM2-135M-Instruct): when the deterministic matcher finds
           several fitting tasks, picks one id from that short list. Output is
           forced to the list by a JSON schema; anything else is ignored. For
           tasks that change something it only produces a "did you mean"
           question; it never starts a change.
extractor  (~0.5B, Qwen2.5-0.5B-Instruct): pulls a package/service/program name
           out of a request. The name must appear word-for-word in what you
           typed, and must then exist on the system; otherwise ignored.
selector   (~0.5B): when an answer has many facts, picks the few that best answer
           your question. Facts are always shown word for word (it cannot
           change or invent text), and warnings (full disk, failures, checks
           that could not run) are always shown whatever it picks.

Backends: an OpenAI-compatible endpoint with JSON-schema output (Ollama, or
llama.cpp's llama-server). Any error, timeout or invalid output -> the aide is
skipped for that request. Nothing is sent anywhere except that local endpoint.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config

CFG = lambda: config.CONFIG_DIR / "aides.json"
DEFAULT = {
    "enabled": False,
    "backend": "ollama",                       # ollama | llamacpp
    "endpoint": "http://127.0.0.1:11434/v1",
    "router_model": "smollm2:135m",
    "helper_model": "qwen2.5:0.5b",
    "llama_server": "llama-server",            # llamacpp backend: started on demand, stopped after use
    "router_gguf": "", "helper_gguf": "",
    "timeout_secs": 20,
}



def load():
    d = dict(DEFAULT)
    f = CFG()
    if f.exists():
        try:
            d.update(json.loads(f.read_text()))
        except (OSError, ValueError):
            d["enabled"] = False
    return d


class Aides:
    def __init__(self, cfg=None):
        self.cfg = cfg or load()
        self._procs = {}
        self.log = []          # what each aide proposed and whether it was accepted (shown by `ask -v`)

    # ------------------------------------------------------------------ plumbing
    def available(self):
        return bool(self.cfg.get("enabled"))

    def _endpoint_for(self, which):
        if self.cfg["backend"] != "llamacpp":
            return self.cfg["endpoint"], self.cfg[f"{which}_model"]
        port = 18431 if which == "router" else 18432
        gguf = self.cfg[f"{which}_gguf"]
        if port not in self._procs:
            exe = shutil.which(self.cfg["llama_server"]) or self.cfg["llama_server"]
            if not gguf or not os.path.exists(gguf) or not os.path.exists(exe):
                raise RuntimeError("llama.cpp server or model file missing")
            p = subprocess.Popen([exe, "-m", gguf, "--host", "127.0.0.1", "--port", str(port), "-c", "2048",
                                  "-t", str(max(1, (os.cpu_count() or 2) // 2)), "--log-disable"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self._procs[port] = p
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                    break
                except OSError:
                    if p.poll() is not None:
                        raise RuntimeError("llama-server exited")
                    time.sleep(0.3)
        return f"http://127.0.0.1:{port}/v1", Path(gguf).stem

    def close(self):
        for p in self._procs.values():
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self._procs.clear()

    def _call(self, which, system, user, schema, max_tokens=120):
        """One schema-constrained completion. Returns the parsed object or None."""
        try:
            url, model = self._endpoint_for(which)
        except Exception as e:
            self.log.append((which, f"unavailable: {e}"))
            return None
        body = {"model": model, "temperature": 0, "max_tokens": max_tokens, "stream": False,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "answer", "strict": True, "schema": schema}}}
        if not url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            self.log.append((which, "refused: aides may only use a local endpoint"))
            return None
        req = urllib.request.Request(url.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.get("timeout_secs", 20)) as r:
                data = json.loads(r.read())
            content = data["choices"][0]["message"]["content"]
            obj = json.loads(content)
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as e:
            self.log.append((which, f"failed: {type(e).__name__}"))
            return None
        if not isinstance(obj, dict):
            self.log.append((which, "rejected: not an object"))
            return None
        return obj

    # ------------------------------------------------------------------ router
    def route(self, text, options):
        ids = [i for i, _ in options]
        listing = "\n".join(f"{i}: {t}" for i, t in options)
        system = ("You map a computer user's request to exactly one task id from a list. "
                  "Answer with the id whose description best matches the request. "
                  "If none fits, answer none.")
        schema = {"type": "object", "additionalProperties": False, "required": ["id"],
                  "properties": {"id": {"type": "string", "enum": ids + ["none"]}}}
        obj = self._call("router", system, f"Tasks:\n{listing}\n\nRequest: {text}\nid:", schema, 20)
        pick = obj.get("id") if obj else None
        ok = pick in ids
        self.log.append(("router", f"proposed {pick!r}: {'accepted' if ok else 'ignored'}"))
        return pick if ok else None

    # ------------------------------------------------------------------ extractor
    def extract_name(self, text, kind):
        system = (f"Copy the {kind} name from the user's request exactly as written. "
                  "Reply with only that name. If there is no clear single name, reply with an empty string.")
        schema = {"type": "object", "additionalProperties": False, "required": ["name"],
                  "properties": {"name": {"type": "string", "maxLength": 64}}}
        obj = self._call("helper", system, text, schema, 30)
        name = (obj or {}).get("name", "")
        name = name.strip() if isinstance(name, str) else ""
        words = re.findall(r"[A-Za-z0-9@._+-]+", text)
        ok = bool(name) and name in words and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._+-]*", name or "-") is not None
        self.log.append(("extractor", f"proposed {name!r}: {'accepted' if ok else 'ignored (not word-for-word in the request)'}"))
        return name if ok else None

    # ------------------------------------------------------------------ selector
    def select_facts(self, question, facts, k=3):
        """Pick the facts that best answer the question. Returns indices (or None).
        The facts themselves are always shown word for word; warnings are always kept."""
        if len(facts) <= k:
            return None
        ids = list(range(len(facts)))
        listing = "\n".join(f"{i}: {f}" for i, f in enumerate(facts))
        system = (f"Pick up to {k} fact numbers that best answer the user's question. "
                  "Reply with the numbers only.")
        schema = {"type": "object", "additionalProperties": False, "required": ["ids"],
                  "properties": {"ids": {"type": "array", "maxItems": k, "uniqueItems": True,
                                         "items": {"type": "integer", "enum": ids}}}}
        obj = self._call("helper", system, f"Question: {question}\nFacts:\n{listing}", schema, 40)
        got = (obj or {}).get("ids")
        ok = isinstance(got, list) and 0 < len(got) <= k and all(isinstance(i, int) and i in ids for i in got) \
            and len(set(got)) == len(got)
        self.log.append(("selector", f"proposed {got!r}: {'accepted' if ok else 'ignored'}"))
        if not ok:
            return None
        keep = list(dict.fromkeys(got + [i for i, f in enumerate(facts) if is_warning(f)]))
        return sorted(keep)


WARNING = re.compile(r"\bFULL\b|nearly full|COULD NOT CHECK|overloaded|has FAILED|not running|"
                     r"no path to the internet|\d+ error messages? logged")


def is_warning(fact):
    if WARNING.search(fact):
        return True
    return fact.lower().startswith("failed") and not fact.rstrip(".").lower().endswith("none")
