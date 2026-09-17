"""Smoke-test the MCP stdio server end-to-end.

Spawns skillmem-mcp, performs JSON-RPC initialize → tools/list → tools/call,
and verifies each round-trip. No external test framework needed.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path


import tempfile

HERE = Path(__file__).resolve().parent.parent
_bin = os.environ.get("SKILLMEM_MCP_BIN") or shutil.which("skillmem-mcp")
SERVER = Path(_bin) if _bin else HERE / ".venv" / "bin" / "skillmem-mcp"
DB_PATH = os.environ.get(
    "SKILLMEM_DB", str(Path(tempfile.gettempdir()) / "skillmem-test" / "memory.db")
)


def _send(proc: subprocess.Popen, payload: dict) -> None:
    line = json.dumps(payload) + "\n"
    assert proc.stdin is not None
    proc.stdin.write(line)
    proc.stdin.flush()


def _read(proc: subprocess.Popen, timeout: float = float(os.environ.get("SMOKE_TIMEOUT", 120))) -> dict:
    assert proc.stdout is not None
    # readline() on a silent server blocks forever, so the deadline has to
    # interrupt the read itself. select() cannot do it here: the bytes may
    # already sit in Python's buffer while the descriptor looks quiet.
    def _bell(_sig, _frm):
        raise TimeoutError("no MCP response")

    old = signal.signal(signal.SIGALRM, _bell)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        while True:
            line = proc.stdout.readline()
            if line:
                return json.loads(line)
            if proc.poll() is not None:        # server died: no point waiting
                raise TimeoutError("server exited without answering")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def main() -> int:
    env = os.environ.copy()
    env["SKILLMEM_DB"] = DB_PATH

    # The script seeds everything it asserts through the server's own stdio,
    # so an empty database is a valid starting point — and no run of this
    # script pulls the caller's real memories into a test database.
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    # stderr goes to a file, never to a pipe nobody drains: on a machine where
    # the embedding model is not cached yet, the download's progress bars fill
    # the pipe buffer and the server blocks writing to it — the run then looks
    # like a server that stopped answering.
    errlog = Path(tempfile.mkdtemp()) / "server.err"
    proc = subprocess.Popen(
        [str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=errlog.open("w"),
        env=env,
        text=True,
        bufsize=1,
    )
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0.1"},
            },
        })
        init = _read(proc)
        assert init.get("result", {}).get("serverInfo", {}).get("name") == "skillmem", init
        print("OK initialize:", init["result"]["serverInfo"])

        _send(proc, {
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}
        })

        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = _read(proc)
        names = sorted(t["name"] for t in tools["result"]["tools"])
        expected = ["mem_archive", "mem_get", "mem_learn", "mem_list", "mem_pin",
                    "mem_recall", "mem_reinforce", "mem_search", "mem_update",
                    "mem_write"]
        assert names == expected, f"got {names}, want {expected}"
        print("OK tools/list:", names)

        # Seed through the same stdio surface: the script has to pass on an
        # empty database, not only on the author's own.
        for i in range(3):
            _send(proc, {
                "jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
                "params": {"name": "mem_write", "arguments": {
                    "slug": f"smoke-feedback-{i}",
                    "title": f"smoke feedback {i}",
                    "body": f"Правило {i}: проверять галлюцинации командой, "
                            f"а не рассуждением — случай номер {i}. "
                            f"Rule {i}: verify claim {i} with a command, not by reasoning.",
                    "kind": "feedback",
                    "check_conflicts": False,   # three near-identical seeds on purpose
                }},
            })
            seeded = json.loads(_read(proc)["result"]["content"][0]["text"])
            assert seeded.get("ok") is True, seeded
        print("OK seeded 3 feedback records")

        # Cyrillic query is intentional: bilingual search is a feature.
        _send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "mem_search", "arguments": {"query": "галлюцинации", "limit": 2}},
        })
        result = _read(proc)
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["count"] >= 1, payload
        print(f"OK mem_search 'галлюцинации': {payload['count']} hit(s)")
        print("    →", payload["results"][0]["slug"])

        _send(proc, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "mem_list", "arguments": {"kind": "feedback", "limit": 3}},
        })
        result = _read(proc)
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["count"] >= 3, payload
        print(f"OK mem_list feedback: {payload['count']} item(s)")

        _send(proc, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {
                "name": "mem_write",
                "arguments": {
                    "slug": "smoke-test-marker",
                    "title": "smoke marker",
                    "body": "Inserted by mcp_smoke.py at run time.",
                    "kind": "note",
                },
            },
        })
        result = _read(proc)
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload.get("ok") is True, payload
        print(f"OK mem_write: slug={payload['slug']} id={payload['id']}")

        return 0
    except (AssertionError, TimeoutError):
        tail = errlog.read_text()[-2000:] if errlog.exists() else ""
        if tail:
            print("--- server stderr (tail) ---\n" + tail, file=sys.stderr)
        raise
    finally:
        proc.terminate()
        # The seeds and the marker are this script's litter: delete them, or a
        # run against a real database leaves four junk records behind. Archiving
        # would not do — the rows would still be there, just hidden.
        try:
            import sqlite3
            db = sqlite3.connect(DB_PATH)
            db.executemany(
                "DELETE FROM memory_items WHERE slug = ?",
                [(s,) for s in ("smoke-test-marker", "smoke-feedback-0",
                                "smoke-feedback-1", "smoke-feedback-2")],
            )
            db.commit(); db.close()
        except Exception as exc:                   # a failed cleanup is not a failed smoke
            print(f"cleanup skipped: {exc}", file=sys.stderr)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
