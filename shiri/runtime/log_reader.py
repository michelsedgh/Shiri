"""Standalone journal reader whose lifetime is tied to its broker parent.

Run with an isolated system interpreter. No network daemon, shell or pager is
started here; the only final payload is the system journal reader.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
from pathlib import Path
import re
import signal
import stat
import sys

UNIT_RE = re.compile(
    r"shiri-(?P<installation>[0-9a-f]{8})-(?P<owner>sender|[0-9a-f]{32})-"
    r"(?P<role>[a-z0-9-]{1,32})-(?P<nonce>[0-9a-f]{32})\.service"
)


def parent_matches(pid: int, birth: str):
    if os.getppid() != pid:
        return False
    try:
        actual = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return False
    return actual == birth and os.getppid() == pid


def death_signal():
    library = ctypes.CDLL(None, use_errno=True)
    function = library.prctl
    function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    function.restype = ctypes.c_int
    if function(1, int(signal.SIGKILL), 0, 0, 0):  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "Cannot bind journal-reader lifetime to its broker parent")


def arm_parent(pid: int, birth: str):
    if type(pid) is not int or pid <= 1 or not birth.isdecimal() or not parent_matches(pid, birth):
        raise ValueError("Journal reader's broker parent is no longer the admitted process")
    death_signal()
    # Parent death can happen between fork, initial validation and prctl. A
    # reparented child must exit even when that death preceded signal arming.
    if not parent_matches(pid, birth):
        raise ValueError("Journal reader lost its broker before lifetime admission")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--birth", required=True)
    parser.add_argument("--unit", required=True)
    options = parser.parse_args(argv)
    if not UNIT_RE.fullmatch(options.unit):
        raise ValueError("Journal reader has no exact owned unit")
    executable = Path("/usr/bin/journalctl")
    descriptor = os.open(executable, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o6022 or not info.st_mode & 0o111):
            raise ValueError("Journal reader executable is not an immutable ordinary root-owned file")
        try:
            os.getxattr(descriptor, "security.capability")
        except OSError as error:
            if error.errno not in {errno.ENODATA, errno.ENOTSUP}:
                raise
        else:
            # Privileged exec clears PDEATHSIG. Even an unexpected file-cap
            # attribute is a refusal, rather than an unobserved orphan risk.
            raise ValueError("Journal reader executable unexpectedly carries file capabilities")
        arm_parent(options.parent, options.birth)
        os.execve(descriptor, [str(executable), "--no-pager", "--follow", "--lines=1000",
                              "--output=short-iso", f"--unit={options.unit}"], os.environ)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"Journal reader refused execution: {error}", file=sys.stderr)
        raise SystemExit(1) from error
