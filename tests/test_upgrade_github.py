"""`skillmem upgrade` via private GitHub Releases + `skillmem token`."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from skillmem import cli as C
from skillmem.cli import main as cli_main


def _run(args: list[str], env: dict | None = None):
    return CliRunner().invoke(cli_main, args, env=env, catch_exceptions=False)


@pytest.fixture
def no_legacy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SKILLMEM_VERSION_URL", raising=False)
    monkeypatch.delenv("SKILLMEM_INSTALL_URL", raising=False)
    monkeypatch.delenv("SKILLMEM_GITHUB_TOKEN", raising=False)


def _fake_release(tag: str, assets: dict[str, bytes]) -> tuple[dict, dict[str, bytes]]:
    meta = {
        "tag_name": tag,
        "assets": [
            {"name": name, "url": f"https://api.github.example/assets/{name}"}
            for name in assets
        ],
    }
    blobs = {f"https://api.github.example/assets/{n}": b for n, b in assets.items()}
    return meta, blobs


def test_upgrade_check_reports_available(no_legacy, monkeypatch: pytest.MonkeyPatch):
    meta, _ = _fake_release("v9.9.9", {})
    monkeypatch.setattr(C, "_github_token", lambda: ("tok", "test"))
    monkeypatch.setattr(C, "_gh_get",
                        lambda url, token, **kw: json.dumps(meta).encode())
    res = _run(["upgrade", "--check"])
    assert res.exit_code == 0
    assert "upgrade available" in res.output
    assert "9.9.9" in res.output


def test_upgrade_requires_token(no_legacy, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(C, "_github_token", lambda: (None, "not found"))
    res = _run(["upgrade", "--check"])
    assert res.exit_code == 2
    assert "token" in res.output.lower()


def test_upgrade_verifies_sha_and_execs_installer(
        no_legacy, monkeypatch: pytest.MonkeyPatch):
    tarball = b"fake-tarball-bytes"
    sha = hashlib.sha256(tarball).hexdigest()
    meta, blobs = _fake_release("v9.9.9", {
        "skillmem-9.9.9.tar.gz": tarball,
        "skillmem-9.9.9.tar.gz.sha256": f"{sha}  skillmem-9.9.9.tar.gz\n".encode(),
        "install.sh": b"#!/bin/bash\n",
        "install.ps1": b"# ps\n",
    })

    def fake_get(url, token, **kw):
        if url.endswith("/releases/latest"):
            return json.dumps(meta).encode()
        return blobs[url]

    monkeypatch.setattr(C, "_github_token", lambda: ("tok", "test"))
    monkeypatch.setattr(C, "_gh_get", fake_get)
    calls: list[list[str]] = []
    monkeypatch.setattr(os, "execvp", lambda prog, argv: calls.append(argv))

    res = _run(["upgrade"])
    assert res.exit_code == 0
    assert "checksum verified" in res.output
    assert calls, "installer was not exec'd"
    argv = calls[0]
    assert argv[0] == "bash" and argv[1].endswith("install.sh")
    assert any(a.startswith("--from=") and a.endswith(".tar.gz") for a in argv)


def test_upgrade_aborts_on_sha_mismatch(no_legacy, monkeypatch: pytest.MonkeyPatch):
    meta, blobs = _fake_release("v9.9.9", {
        "skillmem-9.9.9.tar.gz": b"tampered-bytes",
        "skillmem-9.9.9.tar.gz.sha256": b"deadbeef  skillmem-9.9.9.tar.gz\n",
        "install.sh": b"#!/bin/bash\n",
    })

    def fake_get(url, token, **kw):
        if url.endswith("/releases/latest"):
            return json.dumps(meta).encode()
        return blobs[url]

    monkeypatch.setattr(C, "_github_token", lambda: ("tok", "test"))
    monkeypatch.setattr(C, "_gh_get", fake_get)
    execs: list = []
    monkeypatch.setattr(os, "execvp", lambda *a: execs.append(a))

    res = _run(["upgrade"])
    assert res.exit_code == 1
    assert "mismatch" in res.output.lower()
    assert not execs


def test_token_set_status_clear(memhome: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SKILLMEM_GITHUB_TOKEN", raising=False)
    res = _run(["token", "set", "github_pat_TEST123"])
    assert res.exit_code == 0
    tf = memhome / "github_token"
    assert tf.read_text().strip() == "github_pat_TEST123"

    res = _run(["token", "status"])
    assert "present" in res.output
    assert "github_pat_TEST123" not in res.output  # the value never leaks into output

    res = _run(["token", "clear"])
    assert res.exit_code == 0
    assert not tf.exists()


def test_env_token_wins(memhome: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SKILLMEM_GITHUB_TOKEN", "env-tok")
    tok, source = C._github_token()
    assert tok == "env-tok" and "env" in source
