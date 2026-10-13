"""Setting up and testing the small local models (`steward aides setup|test|off`).

Two routes:
  ollama    uses Ollama (it downloads and verifies models itself).
  llamacpp  uses llama.cpp's llama-server; models are downloaded from Hugging Face
            and each file's SHA-256 is checked against the hash Hugging Face
            publishes for it. A mismatch deletes the download and stops.

After setup, an exam with known right answers runs on YOUR machine. If the models
score below the pass mark, the aides stay switched off and everything works
without them. The scores are saved so you can see them (`steward aides status`).
"""
import hashlib
import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .. import config
from .aides import CFG, DEFAULT, Aides, load

MODELS_DIR = lambda: config.DATA_DIR / "models"
HF = {
    "router": {"repos": ["HuggingFaceTB/SmolLM2-135M-Instruct-GGUF", "bartowski/SmolLM2-135M-Instruct-GGUF"],
               "want": ["q8_0", "Q8_0"], "ollama": "smollm2:135m"},
    "helper": {"repos": ["Qwen/Qwen2.5-0.5B-Instruct-GGUF", "bartowski/Qwen2.5-0.5B-Instruct-GGUF"],
               "want": ["q4_k_m", "Q4_K_M"], "ollama": "qwen2.5:0.5b"},
}
PASS = {"router": 0.8, "extractor": 0.8, "selector": 0.7}

EXAM = {
    "router": [
        ("why is my laptop so slow today", [("health", "check how the computer is doing"), ("memory", "show what is using memory")], {"health", "memory"}),
        ("what's using all my ram", [("memory", "show what is using memory"), ("disk", "show disk space")], {"memory"}),
        ("how full is my drive", [("disk", "show disk space"), ("memory", "show what is using memory")], {"disk"}),
        ("which programs listen on the network", [("ports", "show which programs listen for network connections"), ("network", "check the network connection")], {"ports"}),
        ("did anything crash", [("errors", "show recent errors"), ("services", "list running services")], {"errors"}),
        ("what launches when i log in", [("startup", "show what starts automatically and how long start-up takes"), ("services", "list running services")], {"startup"}),
        ("am i online", [("network", "check the network connection"), ("ports", "show which programs listen for network connections")], {"network"}),
        ("what's hogging space", [("big_files", "find what takes up the most space"), ("memory", "show what is using memory")], {"big_files"}),
        ("show me the software i have", [("packages", "show installed software"), ("services", "list running services")], {"packages"}),
        ("give me a general checkup", [("health", "check how the computer is doing"), ("errors", "show recent errors")], {"health"}),
    ],
    "extractor": [
        ("please put the gimp image editor on here", "package", {"gimp"}),
        ("i want vlc", "package", {"vlc"}),
        ("get rid of that cowsay thing", "package", {"cowsay"}),
        ("restart bluetooth for me", "service", {"bluetooth"}),
        ("close firefox now", "program", {"firefox"}),
        ("is htop on this machine", "package", {"htop"}),
        ("bring back the cups printer service", "service", {"cups"}),
        ("shut the spotify app", "program", {"spotify"}),
        ("add the package ripgrep", "package", {"ripgrep"}),
        ("remove it", "package", {"", None}),
    ],
    "selector": [
        ("how much memory is free", ["On for 2 hours. Workload right now: light.", "Memory: 3.1 GB of 7.8 GB in use, 4.5 GB available.",
                                      "Swap: none.", "Disk /: 43.0 GB used of 118.0 GB (36%), 75.0 GB free.",
                                      "Biggest memory users: firefox 1.2 GB (9 processes)."], {1}),
        ("is my disk full", ["Memory: 3.1 GB of 7.8 GB in use, 4.5 GB available.", "Swap: none.",
                             "Disk /: 43.0 GB used of 118.0 GB (36%), 75.0 GB free.", "Disk /boot: 0.3 GB used of 1.0 GB (30%), 0.7 GB free.",
                             "Failed system services: none."], {2}),
    ],
}


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "steward-aides"})
    return urllib.request.urlopen(req, timeout=timeout)


def hf_pick(role, say=print):
    spec = HF[role]
    for repo in spec["repos"]:
        try:
            with _get(f"https://huggingface.co/api/models/{repo}/tree/main") as r:
                files = json.loads(r.read())
        except Exception as e:
            say(f"   {repo}: not reachable ({type(e).__name__})")
            continue
        for f in files:
            name = f.get("path", "")
            if name.endswith(".gguf") and any(w in name for w in spec["want"]) and f.get("lfs", {}).get("oid"):
                return repo, name, f["lfs"]["oid"], f.get("lfs", {}).get("size") or f.get("size")
        say(f"   {repo}: no matching {spec['want'][0]} file")
    return None


def hf_download(role, say=print):
    pick = hf_pick(role, say)
    if not pick:
        raise RuntimeError(f"could not find a {role} model file on Hugging Face")
    repo, name, sha, size = pick
    dest = MODELS_DIR() / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == sha:
        say(f"   {name}: already downloaded and verified")
        return str(dest)
    part = dest.with_suffix(dest.suffix + ".part")
    say(f"   downloading {repo}/{name} ({(size or 0) / 1e6:.0f} MB)...")
    h = hashlib.sha256()
    with _get(f"https://huggingface.co/{repo}/resolve/main/{name}", timeout=120) as r, open(part, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            out.write(chunk)
    if h.hexdigest() != sha:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"CHECKSUM MISMATCH for {name}: download deleted, nothing installed")
    part.rename(dest)
    say(f"   verified SHA-256 {sha[:16]}… matches Hugging Face's published hash")
    return str(dest)


def save(cfg):
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CFG().write_text(json.dumps(cfg, indent=2) + "\n")


def setup(backend, say=print):
    cfg = dict(DEFAULT) | load()
    cfg["backend"] = backend
    cfg["enabled"] = False
    if backend == "ollama":
        if not shutil.which("ollama"):
            raise RuntimeError("Ollama is not installed. See https://ollama.com/download (Fedora: the official "
                               "install script or the 'ollama' package if your release has it).")
        for role in ("router", "helper"):
            tag = HF[role]["ollama"]
            say(f"   ollama pull {tag} (Ollama verifies the download itself)")
            r = subprocess.run(["ollama", "pull", tag])
            if r.returncode != 0:
                raise RuntimeError(f"ollama pull {tag} failed")
            cfg[f"{role}_model"] = tag
        cfg["endpoint"] = "http://127.0.0.1:11434/v1"
    else:
        exe = shutil.which(cfg.get("llama_server") or "llama-server")
        if not exe:
            raise RuntimeError("llama-server not found. Try: sudo dnf install llama-cpp  (or use --backend ollama)")
        cfg["llama_server"] = exe
        for role in ("router", "helper"):
            cfg[f"{role}_gguf"] = hf_download(role, say)
    save(cfg)
    return exam(cfg, say)


def exam(cfg=None, say=print):
    """Run the fixed exam on this machine. Enables the aides only if every part passes."""
    cfg = dict(cfg or load())
    cfg["enabled"] = True
    a = Aides(cfg)
    scores = {}
    t0 = time.time()
    try:
        ok = sum(1 for q, opts, good in EXAM["router"] if a.route(q, opts) in good)
        scores["router"] = ok / len(EXAM["router"])
        ok = sum(1 for q, kind, good in EXAM["extractor"] if (a.extract_name(q, kind) or "") in {g or "" for g in good})
        scores["extractor"] = ok / len(EXAM["extractor"])
        ok = 0
        for q, facts, good in EXAM["selector"]:
            pick = a.select_facts(q, facts, k=2)
            if pick and good <= set(pick):
                ok += 1
        scores["selector"] = ok / len(EXAM["selector"])
    finally:
        a.close()
    passed = all(scores[k] >= PASS[k] for k in PASS)
    cfg["enabled"] = passed
    cfg["exam"] = {"scores": scores, "pass_marks": PASS, "passed": passed, "secs": round(time.time() - t0, 1),
                   "when": time.strftime("%Y-%m-%d %H:%M")}
    save(cfg)
    for k, v in scores.items():
        say(f"   {k:<10} {v:.0%} (pass mark {PASS[k]:.0%})")
    say("   aides ENABLED" if passed else "   aides stay OFF: below the pass mark. Everything still works "
        "without them (deterministic code only).")
    return passed, scores
