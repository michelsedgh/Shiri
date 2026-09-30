# Candidate installation and service-state adoption

The rebuild is a candidate. Complete fresh-host installation, native Cast
input, phone/group interoperability, final-speaker timing and room-daemon
privilege separation remain required gates. The dedicated candidate service
harness tests a staged installation, rather than the full apt/build/install
sequence on an empty host.

Run installation from the reviewed checkout as root on 64-bit Linux. Use a
canonical absolute prefix such as `/opt/shiri`; custom prefixes are supported.
The prefix and its ancestors must be actual root-owned directories without
group/world write permission. Every existing executable-tree member is
checked before privileged pip/build/service use; produced files are checked
again. Scripts set umask 022, use system tools for the initial validation, and
do not execute a validator from the prefix being checked.

Root-owned venv interpreter links to trusted regular system executables and
internal directory links such as `lib64 -> lib` are supported. Writable or
foreign-owned members, prefix/ancestor symlinks, external directory links,
special files and set-ID files are refused. A refusal preserves the tree;
investigate it or choose a new trusted prefix. Do not broadly chown an
untrusted tree to make it pass. The `shiri` API account also needs traversal
and read access to the installed package; service installation tests its
import without granting that account write access to the package.

The reviewed backend commits are fixed in `build_backends.sh`. Python runtime
packages and hashes are fixed in `requirements.lock`. Service installation
does not start the default units or replace an existing API token. Stop the
services and every manual process using the installation before updating an
existing executable tree; installation does not make a running daemon switch
versions atomically. Review `/etc/shiri/shiri.env` before explicit activation.

## Local ALSA endpoints

Configure a named card, for example
`plughw:CARD=KitchenDAC,DEV=0,SUBDEV=0` or
`hw:CARD=Loopback,DEV=1,SUBDEV=7`. `plughw` retains ALSA format/rate
conversion; `hw` opens the hardware format directly. Numeric `DEV` and
`SUBDEV` values are supported. Numeric **CARD** values such as `plughw:7,0`
are refused during API admission, stored-state validation and runtime
startup because enumeration can send a saved route to a different device
after reconnect or reboot.

An existing database containing a numeric CARD is preserved and refused.
Stop all users, back up the main file and companions together, identify the
intended physical device, and repair a separate reviewed copy to use its
named endpoint. The legacy importer also requires an operator-reviewed
named copy; it does not guess the current device from an old card index.

A named CARD is not proof of physical identity. ALSA can assign suffixes to
colliding names, so identical USB adapters can exchange names when discovery
order changes. Unique hardware identity enrollment, ambiguity rejection and
verification at each PCM open remain required for that case. The candidate
has not implemented that binding and must not be treated as guaranteeing
physical-device identity for automatically named USB cards. See the
[kernel card-name handling](https://github.com/torvalds/linux/blob/v6.8/sound/core/init.c#L632-L709).

## Existing database and credentials

`deploy/install_services.sh` holds a root-only installation lock and requires
`shiri-api.service` to be stopped. Existing state must belong to root or the
validated non-login `shiri` account; linked, hardlinked, writable or unknown
files/directories are refused. Existing token contents are validated without
printing or replacing them. Unit and environment targets must be regular,
unlinked, root-owned files with safe permissions.

SQLite adoption changes ownership only on an exactly held main-file
descriptor after read-only validation. A Linux open-file-description byte
lock blocks SQLite writers during validation/adoption, and another process
holding even an idle main-file descriptor causes refusal. The helper creates
no WAL/SHM and does not use immutable validation to interpret a live WAL.

Any `shiri.sqlite3-wal`, `-shm` or `-journal` file, including an empty file or
dangling symlink, causes refusal. This also applies when the main file is
missing or empty. The installer preserves all those files and never fixes
the failure by deleting or changing companion ownership. Stop every manual
API/SQLite process as well as the unit, preserve a backup containing the main
file and all companions, and investigate missing/empty-main cases before
attempting recovery.

For an existing valid main database with retained WAL, verify that the main
and companions are regular, unlinked files under the expected directory and
belong to the known current owner. Do not run SQLite as root on unknown files
to bypass an ownership refusal. After stopping all writers, run a checkpoint
as that verified owner (`root` or `shiri` for an admissible installation):

```sh
sudo systemctl stop shiri-api
stat -c '%U %a %h %F' /var/lib/shiri/shiri.sqlite3
# Replace CURRENT_OWNER with the verified existing owner, root or shiri.
sudo -u CURRENT_OWNER /usr/bin/python3 -I - <<'PY'
import os
import sqlite3
import stat

path = '/var/lib/shiri/shiri.sqlite3'
descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
try:
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or info.st_mode & 0o022
            or os.read(descriptor, 16) != b'SQLite format 3\0'):
        raise SystemExit('Unexpected main file; preserve it and investigate')
finally:
    os.close(descriptor)
for suffix in ('-wal', '-shm', '-journal'):
    try:
        info = os.lstat(path + suffix)
    except FileNotFoundError:
        continue
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise SystemExit('Unexpected companion file; preserve it and investigate')
# mode=rw refuses to create a missing main file.
connection = sqlite3.connect('file:' + path + '?mode=rw', uri=True, timeout=5)
try:
    result = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    if result[0] != 0:
        raise SystemExit('Checkpoint is busy; preserve all files and stop remaining writers')
finally:
    connection.close()
if any(os.path.lexists(path + suffix) for suffix in ('-wal', '-shm', '-journal')):
    raise SystemExit('Companions remain; preserve them and inspect remaining processes')
PY
```

This is an explicit operator recovery step, not automatic installer behavior.
Rerun service installation only after the connection is closed and companions
are absent. An unresolved journal, corrupt database, unknown owner or changed
file must be investigated; no deletion or forced ownership bypass is provided.

The repeatable disposable Linux boundary checks are described in
[../tests/linux/README.md](../tests/linux/README.md). They do not install
packages, build backends, touch default units or adopt production state.
All 16 boundary checks passed on the Ubuntu candidate on September 30, 2026,
including real OFD lock persistence, idle-connection refusal and exact
companion-free adoption. This evidence does not close the fresh-host
installation gate.
