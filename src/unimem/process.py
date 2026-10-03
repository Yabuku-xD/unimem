"""Process ancestry, used to tell concurrent tool sessions apart."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache

MAX_DEPTH = 16


@lru_cache(maxsize=1)
def ancestor_pids() -> tuple[int, ...]:
    """Parent, grandparent, and so on of this process, nearest first.

    A coding tool starts both its session hook and its unimem server, so the
    two share the tool's process as an ancestor. Returns an empty tuple when
    the process table cannot be read.
    """
    try:
        output = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    parents: dict[int, int] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            parents[int(fields[0])] = int(fields[1])
    chain: list[int] = []
    pid = os.getppid()
    # pid 1 adopts every orphan, so it identifies nothing.
    while pid > 1 and len(chain) < MAX_DEPTH and pid not in chain:
        chain.append(pid)
        pid = parents.get(pid, 0)
    return tuple(chain)
