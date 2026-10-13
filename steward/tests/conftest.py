"""Test setup: a throwaway home folder, fake systemctl/pkexec/dnf/rpm/zenity/flatpak,
so destructive behaviour is only ever exercised on disposable data."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# not under /tmp: the read-only sandbox mounts a private /tmp
BASE = Path(tempfile.mkdtemp(prefix=".steward-test-", dir=os.path.expanduser("~")))
HOME = BASE / "home"
FAKE = BASE / "fake"
FAKEBIN = ROOT / "tests" / "fakebin"
os.environ.update({
    "STEWARD_TEST_HOME": str(HOME), "HOME": str(HOME),
    "STEWARD_DATA": str(HOME / ".local/share/steward"),
    "STEWARD_CONFIG": str(HOME / ".config/steward"),
    "FAKE_STATE": str(FAKE), "PATH": f"{FAKEBIN}:{os.environ['PATH']}",
    "STEWARD_PASS_ENV": "FAKE_STATE,FAKE_PKEXEC,FAKE_ZENITY",
    "DISPLAY": ":99",
    "STEWARD_OS_RELEASE": str(ROOT / "tests" / "os-release" / "fedora"),
    "OPENROUTER_API_KEY": "sk-or-v1-THISISAFAKEKEYTHATMUSTNEVERLEAK1234",
})
os.environ.pop("WAYLAND_DISPLAY", None)
sys.path.insert(0, str(ROOT))

import atexit  # noqa: E402

import pytest  # noqa: E402

atexit.register(shutil.rmtree, BASE, True)

POLICY = {"require_zeroclaw_gate": False, "approval_mode": "desktop", "approval_timeout_secs": 5,
          "wait_secs": 60}


def build_home(policy=None):
    if HOME.exists():
        shutil.rmtree(HOME)
    shutil.rmtree(FAKE, ignore_errors=True)
    FAKE.mkdir(parents=True)
    for d in ["Downloads/models", ".cache/thing", "Documents", ".ssh", ".zeroclaw", ".config/steward",
              ".local/share/steward"]:
        (HOME / d).mkdir(parents=True, exist_ok=True)
    (HOME / "Downloads/models/big.gguf").write_bytes(b"x" * 5000)
    (HOME / "Downloads/note.txt").write_text("hello\n")
    (HOME / "Documents/thesis.docx").write_text("precious\n")
    (HOME / "Documents/app.conf").write_text("mode=slow\ncache=on\n")
    (HOME / ".ssh/id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"
                                          "-----END OPENSSH PRIVATE KEY-----\n")
    (HOME / ".zeroclaw/config.toml").write_text('api_key = "sk-or-v1-secretsecretsecret99"\n')
    (HOME / ".config/steward/policy.json").write_text(json.dumps(policy or POLICY))
    (HOME / ".bashrc").write_text("# rc\n")


@pytest.fixture
def eng(tmp_path, monkeypatch):
    build_home()
    monkeypatch.setenv("FAKE_ZENITY", "approve")
    from steward.engine import Engine
    from steward.store import Store
    e = Engine(store=Store(tmp_path / "steward.db"))
    yield e
    for _ in range(100):
        if not e.active:
            break
        import time
        time.sleep(0.1)
