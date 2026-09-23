"""Exercise the Docker entrypoint with an MCP client that keeps stdin open."""

import json
from pathlib import Path
import queue
import subprocess
import sys
import threading


def test_docker_entrypoint_starts_server_before_stdin_eof(memhome):
    root = Path(__file__).resolve().parents[1]
    line = next(line for line in (root / "Dockerfile").read_text().splitlines()
                if line.startswith("ENTRYPOINT "))
    entrypoint = json.loads(line.removeprefix("ENTRYPOINT "))
    if entrypoint == ["skillmem", "mcp"]:
        # Observe the server launch boundary independently of SDK transport.
        command = [sys.executable, "-P", "-c",
                   "import skillmem.mcp_server as server; "
                   "server.run = lambda: print('server started', flush=True); "
                   "from skillmem.cli import main; main()", "mcp"]
    else:
        # Resolve the published image's COPY destination to its source file.
        assert entrypoint == ["python", "/usr/local/bin/mcp-stdio-shim.py"]
        command = [sys.executable, "-P", str(root / "docker/mcp-stdio-shim.py")]

    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=root)
    replies = queue.Queue()
    reader = threading.Thread(target=lambda: replies.put(proc.stdout.readline()),
                              daemon=True)
    reader.start()
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "regression-test", "version": "1"}},
        }) + "\n")
        proc.stdin.flush()
        try:
            reply = replies.get(timeout=10)
        except queue.Empty:
            raise AssertionError("MCP server launch blocked while stdin stayed open")
        assert reply.strip() == "server started", reply
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=5)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
