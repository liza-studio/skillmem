"""Smoke-test the MCP stdio server end-to-end.

Spawns skillmem-mcp, performs JSON-RPC initialize → tools/list → tools/call,
and verifies each round-trip. No external test framework needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


import tempfile

HERE = Path(__file__).resolve().parent.parent
SERVER = HERE / ".venv" / "bin" / "skillmem-mcp"
DB_PATH = os.environ.get(
    "SKILLMEM_DB", str(Path(tempfile.gettempdir()) / "skillmem-test" / "memory.db")
)


def _send(proc: subprocess.Popen, payload: dict) -> None:
    line = json.dumps(payload) + "\n"
    assert proc.stdin is not None
    proc.stdin.write(line)
    proc.stdin.flush()


def _read(proc: subprocess.Popen, timeout: float = 5.0) -> dict:
    assert proc.stdout is not None
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line:
            return json.loads(line)
    raise TimeoutError("no MCP response")


def main() -> int:
    env = os.environ.copy()
    env["SKILLMEM_DB"] = DB_PATH

    # Make sure the smoke DB is populated — fresh check-out shouldn't need
    # the caller to run `skillmem migrate` first.
    db = Path(DB_PATH)
    db.parent.mkdir(parents=True, exist_ok=True)
    if not db.exists() or db.stat().st_size < 1024:
        migrator = HERE / ".venv" / "bin" / "skillmem"
        subprocess.run(
            [str(migrator), "migrate"],
            env={**os.environ, "SKILLMEM_DB": DB_PATH},
            check=True,
            capture_output=True,
        )

    proc = subprocess.Popen(
        [str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
        expected = ["mem_get", "mem_learn", "mem_list", "mem_recall", "mem_reinforce",
                    "mem_search", "mem_update", "mem_write"]
        assert names == expected, f"got {names}, want {expected}"
        print("OK tools/list:", names)

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
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
