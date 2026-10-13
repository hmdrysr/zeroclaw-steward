"""Distribution detection and package steps on Fedora, Debian/Ubuntu, Arch and openSUSE
(fake package tools; real detection logic)."""
import json

import pytest

from conftest import FAKE, HOME, ROOT
from steward import distro

OSR = ROOT / "tests" / "os-release"


@pytest.mark.parametrize("name,pkg,pretty", [
    ("fedora", "dnf", "Fedora Linux 43"), ("alma", "dnf", "AlmaLinux"), ("ubuntu", "apt", "Ubuntu"),
    ("mint", "apt", "Linux Mint"), ("arch", "pacman", "Arch"), ("endeavour", "pacman", "EndeavourOS"),
    ("tumbleweed", "zypper", "openSUSE"), ("gentoo", "unknown", "Gentoo"),
])
def test_detect(name, pkg, pretty):
    d = distro.detect(str(OSR / name))
    assert d.pkg == pkg and pretty in d.name


def pkgs():
    return set(json.load(open(FAKE / "pkgs.json")))


@pytest.mark.parametrize("osr", ["fedora", "ubuntu", "arch", "tumbleweed"])
def test_install_remove_and_undo_each_distro(eng, monkeypatch, osr):
    monkeypatch.setenv("STEWARD_OS_RELEASE", str(OSR / osr))
    out = eng.submit("g", [{"do": "pkg_install", "packages": ["ripgrep"]}], verify=["true"])
    assert "VERIFIED" in out and "ripgrep" in pkgs()
    out = eng.submit("g", [{"do": "pkg_remove", "packages": ["cowsay"]}], verify=["true"])
    assert "VERIFIED" in out and "cowsay" not in pkgs()
    rid = eng.store.q1("SELECT id FROM runs WHERE do='pkg_remove'")[0]
    out = eng.submit("undo", [{"do": "undo", "ref": f"r{rid}"}], verify=["true"])
    assert "VERIFIED" in out and "cowsay" in pkgs()


@pytest.mark.parametrize("osr", ["fedora", "ubuntu", "arch", "tumbleweed"])
def test_dependents_detected_each_distro(eng, monkeypatch, osr):
    monkeypatch.setenv("STEWARD_OS_RELEASE", str(OSR / osr))
    out = eng.submit("g", [{"do": "pkg_remove", "packages": ["libfoo"]}], verify=["true"])
    assert "NEEDS FIX" in out and "foo-app" in out and "libfoo" in pkgs()


@pytest.mark.parametrize("osr,core", [("fedora", "glibc"), ("ubuntu", "libc6"), ("arch", "glibc"),
                                      ("ubuntu", "linux-image-generic"), ("arch", "pacman")])
def test_core_packages_refused_each_distro(eng, monkeypatch, osr, core):
    monkeypatch.setenv("STEWARD_OS_RELEASE", str(OSR / osr))
    out = eng.submit("g", [{"do": "pkg_remove", "packages": [core]}], verify=["true"])
    assert "NEEDS FIX" in out and ("PROHIBITED" in out or "already absent" in out)


def test_unknown_distro_refuses_package_steps(eng, monkeypatch):
    monkeypatch.setenv("STEWARD_OS_RELEASE", str(OSR / "gentoo"))
    out = eng.submit("g", [{"do": "pkg_install", "packages": ["htop2"]}], verify=["true"])
    assert "NEEDS FIX" in out and "not supported" in out


def test_dnf_alias_rejected_on_ubuntu(eng, monkeypatch):
    monkeypatch.setenv("STEWARD_OS_RELEASE", str(OSR / "ubuntu"))
    out = eng.submit("g", [{"do": "dnf_install", "packages": ["htop2"]}], verify=["true"])
    assert "NEEDS FIX" in out and "pkg_install" in out


def test_missing_repo_package_fails_honestly(eng, monkeypatch):
    monkeypatch.setenv("STEWARD_OS_RELEASE", str(OSR / "ubuntu"))
    out = eng.submit("g", [{"do": "pkg_install", "packages": ["nosuchthing"]}], verify=["true"])
    assert "NEEDS INPUT" in out and "Unable to locate package" in out
