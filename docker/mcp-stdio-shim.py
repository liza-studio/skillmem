#!/usr/bin/env python3
"""Hold stdin open long enough for the server to read what was already sent.

Why this exists: a scanner (Glama's, for one) starts the container, writes
`initialize`, `notifications/initialized` and `tools/list` in one go, and closes
stdin immediately. Importing the MCP SDK alone takes ~0.4s, so the reader does
not exist yet when EOF lands, and only the first request is answered. The
catalogue listing then shows zero tools for a server that has nine.

Measured in the container, cold start each time:

    batch then immediate EOF   -> initialize answered, tools/list never
    warm server, same batch    -> both answered
    batch, stdin held open     -> both answered in 0.7s

So the fix is not to make the server faster (the 0.4s is the SDK's own import),
but to stop throwing away bytes that already arrived: drain stdin into memory
first, start the server, feed it, and keep the pipe open a moment afterwards.

Only the image uses this. The published package and every ordinary client —
Claude Code, Codex, Cursor — talk to `skillmem mcp` directly and are unaffected.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

GRACE_SECONDS = float(os.environ.get("SKILLMEM_STDIO_GRACE", "3"))


def main() -> int:
    # Read everything the client sent, EOF included. A closed stdin does not
    # discard the bytes already in the pipe; the server just never got to them.
    data = sys.stdin.buffer.read()

    proc = subprocess.Popen(["skillmem", "mcp"], stdin=subprocess.PIPE)

    def feed() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(data)
                proc.stdin.flush()
            # Give the server time to answer what it was just handed before the
            # pipe closes under it. A client that keeps talking is unaffected:
            # it never reaches this path, because its stdin does not end.
            time.sleep(GRACE_SECONDS)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass

    threading.Thread(target=feed, daemon=True).start()
    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
