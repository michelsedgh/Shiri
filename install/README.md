# Candidate installation and service-state adoption

The rebuild is a candidate. Complete fresh-host installation, native Cast
input, phone/group interoperability and physical final-speaker timing remain
required gates. Distinct unprivileged room workers, exact device/ioctl limits,
socket publication and recovery have passed dedicated real-kernel checks.
Those checks and the staged service harness do not cover the full
apt/build/install sequence on an empty host.

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

The reviewed backend commits and maintained patches are fixed in
`build_backends.sh`. Python runtime packages and hashes are fixed in
`requirements.lock`. Package-build tools have a separate
`build_requirements.lock`: [Hatchling 1.32.4](https://pypi.org/project/hatchling/1.32.4/)
and its complete Python 3.10 dependency graph are pinned to verified universal
wheel hashes. The installer installs build and runtime dependencies as wheels
with hash verification, refusing source-only dependency builds,
then builds the reviewed source with `--no-index --no-build-isolation --no-deps`.
Package metadata/build steps cannot silently download an unpinned backend or
resolve additional dependencies. Updating the backend requires reviewing both
the exact `pyproject.toml` build-system pin and the build lock.

The packaging regressions run without root, apt or service changes. Prepare
the verified build wheelhouse once, then run the checks with all package indexes
disabled inside disposable virtual environments:

```sh
python3 -m pip download --require-hashes --only-binary=:all: \
  -r install/build_requirements.lock -d /tmp/shiri-build-wheelhouse
SHIRI_BUILD_WHEELHOUSE=/tmp/shiri-build-wheelhouse \
  .venv/bin/python -m pytest -q tests/test_package_build.py
```

The wheelhouse must contain the seven exact universal wheels admitted by the
lock. The checks exercise the installer's actual package commands, inspect the
installed CLI and web assets, refuse a tampered wheel, reject a source-only
runtime dependency, and verify a missing backend produces no build-index request.
Without the wheelhouse, normal pytest runs the lock/command checks and explicitly
skips the actual offline build cases. The project test environment is a separate
prerequisite; these commands do not install pytest.

Installation requires Python 3.10 or newer, systemd with cgroup v2, the system
GI/GStreamer audio typelibs and plugins, and the Linux networking/ALSA tools
installed by the script. `install/install.sh --with-backends` builds the pinned
patched daemons; without that option, provision a reviewed compatible backend
tree before activation. Stock OwnTone, Shairport or Avahi binaries do not satisfy
the candidate feature checks. The default scripts require apt/Python package
access; an offline rehearsal needs complete verified apt and wheel mirrors,
plus pinned backend source archives if building them. Service installation
does not start the default units or replace an existing API token. Stop the
services and every manual process using the installation before updating an
existing executable tree; installation does not make a running daemon switch
versions atomically. Review `/etc/shiri/shiri.env` before explicit activation.

The separate private cold-speech proposal adds the required `-ready1` OwnTone
feature and its authenticated idle-output/first-dispatch preparation endpoint.
Its Python consumer and backend must be installed together; an older `-speech1`
binary is refused before room startup. Its five-second setup limit does not
qualify audible latency or cold utterance completeness. It has portable C and
control proofs, with fresh Linux compilation and finite audio proof still pending.

Dependency apt commands run under `install/apt_dependencies.py`. It temporarily
installs a deny-all `policy-rc.d` and selects `needrestart` list-only mode. A
trusted existing policy is restored with its original inode and contents,
including when apt fails. The helper serializes its changes with a root-only
lock and refuses linked, writable or foreign policies. If interrupted, its
policy contains the saved backup location; investigate and restore that policy
before retrying. A concurrently changed policy is preserved rather than
overwritten. This controls Debian package service hooks; it does not make
updating an actively used binary tree safe.

## Fixed worker identities and upgrades

The installer publishes one root-only version-2 daemon map: the existing 42
receiver/output/audio/discovery/timing identities plus eight distinct Bluetooth
bridge identities. Every UID and primary GID is unique and bound to the same
installation UUID. The map also fixes its state directory and broker-lock
directory. Another installation cannot adopt these accounts.

An existing version-1 map needs a stopped, quiescent upgrade. Stop its actual
broker and all of its owned units first, and recover any retained resources
with the version that admitted them. The installer holds the exact owning
`broker.lock`, verifies an empty ownership manifest, and scans live processes
for every managed real/effective/saved/filesystem UID before atomically
publishing version 2. It preserves all 42 published UID/GID assignments and
never deletes or reassigns existing accounts. A failed publication may leave
new, correctly marked non-login accounts; a safe retry validates and adopts
those accounts while preserving the old map.

For a dedicated candidate using custom paths, invoke the isolated system
interpreter with those exact owning directories:

```sh
/usr/bin/python3 -I deploy/provision_daemons.py \
  --output /etc/shiri-v2-test/daemon-identities.json \
  --installation-id <existing-ownership-manifest-UUID> \
  --runtime-state-dir /var/lib/shiri-v2-test-runtime \
  --runtime-dir /run/shiri-v2-test
```

Do not point migration at a new empty lock directory. If the map, account,
lock inode or owned manifest changes, installation stops and preserves the
publication for inspection. The new broker refuses a version-1 map.

The source replacement Bluetooth profile uses a separate bridge UID, only
AF_UNIX, and no host D-Bus connection, audio group or ALSA node. The root broker
admits an exact BlueALSA device and hands over only its PCM/control descriptors.
The former same-UID host-bus bridge is available for exact saved-state cleanup;
new launches of that route are refused. The replacement framed backend/worker
and actual descriptor, Drop, recovery and physical Bluetooth checks must pass
before enabling it. `SHIRI_INSTALL_BLUETOOTH=1` only installs optional distro
Bluetooth tooling; it does not supply the required maintained SBC
`OpenRestricted`/synchronous `DropSync` BlueALSA capability. Stock BlueALSA is
refused by the descriptor-only admission path.

## Local ALSA endpoints

Enroll physical cards from the authenticated device inventory, choosing an
unambiguous hardware serial identity or an explicit physical-port binding.
Rooms save the resulting `shiri:device=<UUID>` endpoint. The broker revalidates
that binding at launch, holds its sysfs identity, and supplies a private manifest
for OwnTone to verify the actual opened PCM. A named physical endpoint such as
`plughw:CARD=KitchenDAC,DEV=0,SUBDEV=0` is insufficient and is refused at launch;
constrained named virtual Loopback endpoints remain available for tests.
`plughw` conversion is retained through the enrollment preference. Numeric `DEV`
and `SUBDEV` values are supported. Numeric **CARD** values such as `plughw:7,0`
are refused during API admission, stored-state validation and runtime
startup because enumeration can send a saved route to a different device
after reconnect or reboot.

An existing database containing a numeric CARD is preserved and refused.
Stop all users, back up the main file and companions together, identify the
intended physical device, and repair a separate reviewed copy to use its
named endpoint for review and enroll the intended physical hardware before
enabling it. The legacy importer requires an operator-reviewed named copy;
it does not guess the current device from an old card index.

A named CARD is not proof of physical identity. ALSA can assign suffixes to
colliding names, so identical USB adapters can exchange names when discovery
order changes. Unique hardware identity enrollment, ambiguity rejection and
verification at each PCM open are implemented in the candidate source but
combined real-device enrollment, reconnection and wrong-device refusal checks
remain required. Do not treat automatically named USB cards as guaranteeing
physical-device identity. See the
[kernel card-name handling](https://github.com/torvalds/linux/blob/v6.8/sound/core/init.c#L632-L709).

The current Linux check also found that libasound requires a card control open
even for an integer PCM card selector. The candidate admits the matching control
node only behind its mandatory inherited ioctl filter, which allows the two PCM
open operations and denies hardware mixer/TLV changes, compat/x32 and io_uring.
Actual successful-open and denied-mixer-write checks must pass before candidate
service activation; the standalone production helper has passed those checks
on the candidate VM. Combined broker/native-playback/recovery checks remain
required. The decoder never executes if filter installation fails.

`install/build_helpers.sh` compiles the broker's persistent cgroup bind-policy
helper into the backend prefix's `libexec` directory. OwnTone stays behind an
immutable unprivileged launch gate until the broker verifies and durably records
the policy. `SHIRI_BIND_POLICY_HELPER` can select a separately reviewed trusted
helper path. Missing helpers, writable installation ancestors or overlapping
sender automatic/control port ranges refuse launch.
The same installer builds `libexec/shiri-pcm-exec`; `SHIRI_PCM_EXEC_HELPER` can
select a separately reviewed trusted copy. Both helpers are ordinary root-owned
executables with no set-ID bit. The bind helper executes as root; the PCM helper
requires an already unprivileged, capability-free output identity.

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

The descriptor-only Bluetooth route requires the maintained BlueALSA
`OpenRestricted`/completed `DropSync` capability, currently limited to SBC
A2DP output. Stock BlueALSA fails admission. Stage its separate private-prefix
candidate with `install/build_bluealsa.sh`; the script does not activate a host
service or install host D-Bus policy. Build, service and paired-device acceptance
must pass before that route is promoted. See the [patch contract and checks](patches/README.md#bluealsa-synchronous-restricted-controller).

The private first-anchor deadline proposal adds `-anchor1` after `-speech1-ready1`.
It refreshes the player clock after a potentially waiting input peek and fails
closed if the original presentation anchor or existing admission budget has
elapsed. It requires a newly reviewed backend build; earlier ready1 binaries
cannot qualify. Controlled C admission tests do not replace final PCM timing
checks or establish physical receiver support.


The additive speech-owner layer follows the unchanged fourteen-layer jitter1
backend and appends `-owner1` to its exact version. The builder verifies
`owntone-29.3-speech-owner.patch`, executes prior-layer checks before applying
it, then runs the owner media/player/JSON and Linux credential checks before
building. The runtime requires that exact coherent marker; earlier binaries
cannot admit the v2/96byte producer. BEGIN and exact observed retirement reuse
the authenticated nine-field readiness request and existing speech_id. The
layer preserves original250ms packet expiry,20ms reserve, natural EOF tail,
source/output transport files and music presentation/buffering policy. See
[the voice owner contract](../docs/SPEECH_JITTER.md). Portable sanitizer proofs
do not establish a built Linux backend or physical output acceptance.
