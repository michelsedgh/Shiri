#!/usr/bin/python3
"""Linux-only real parent-death check, with no units, namespaces or audio.

Forks only this harness's two private processes. Exact pidfds own every signal.
Run from the checkout using the candidate venv; produces a small JSON report.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shiri.runtime.log_reader import arm_parent  # noqa: E402
from shiri.runtime.system import process_birth  # noqa: E402


def main():
    if not sys.platform.startswith("linux") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("This isolated process check requires Linux pidfds")
    output, ready = os.pipe()
    broker = os.fork()
    if broker == 0:
        os.close(output)
        receiver = os.fork()
        if receiver == 0:
            try:
                null = os.open("/dev/null", os.O_RDWR)
                os.dup2(null, 1)
                os.dup2(null, 2)
                os.close(null)
                parent = os.getppid()
                arm_parent(parent, process_birth(parent))
                os.write(ready, json.dumps({"pid": os.getpid(), "birth": process_birth(os.getpid())}).encode() + b"\n")
                while True:
                    signal.pause()
            finally:
                os._exit(1)
        os.close(ready)
        while True:
            signal.pause()
    os.close(ready)
    parent_fd = os.pidfd_open(broker)
    follower_fd = None
    try:
        readable, _, _ = select.select([output], [], [], 5)
        if not readable:
            raise RuntimeError("Private journal-lifetime child never armed")
        record = json.loads(os.read(output, 4096))
        follower_fd = os.pidfd_open(record["pid"])
        if process_birth(record["pid"]) != record["birth"]:
            raise RuntimeError("Private child identity changed before parent-death test")
        signal.pidfd_send_signal(parent_fd, signal.SIGKILL)
        readable, _, _ = select.select([follower_fd], [], [], 5)
        if not readable:
            raise RuntimeError("Kernel allowed the log follower to outlive its killed broker")
        os.waitpid(broker, 0)
        return {"ok": True, "parent_sigkill": True, "follower_exited": True, "signals_used_pidfds": True,
                "stdout_devnull": True, "subject": "armed idle child"}
    finally:
        for descriptor in [parent_fd, follower_fd]:
            if descriptor is not None:
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.close(descriptor)
        os.close(output)
        try:
            os.waitpid(broker, 0)
        except ChildProcessError:
            pass


if __name__ == "__main__":
    print(json.dumps(main()))
