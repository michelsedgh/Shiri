"""Root-prepared private file views for fixed-identity daemon services.

The enclosing installation and room directories remain root-only. A service
receives explicit mounts; changing generated paths never grants parent access.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
from xml.sax.saxutils import escape

from .system import RuntimeFailure
from .units import Bind, VIEW, path_text


def directory(path: Path, account: dict | None = None, *, mode=0o700):
    path_text(path)
    uid, gid = (account["uid"], account["gid"]) if account else (0, 0)
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=mode)
        info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, uid}
            or info.st_gid not in {0, gid}):
        raise RuntimeFailure("A private daemon directory changed ownership or type")
    os.chown(path, uid, gid, follow_symlinks=False)
    path.chmod(mode)
    current = path.lstat()
    if (not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
            or current.st_uid != uid or current.st_gid != gid
            or stat.S_IMODE(current.st_mode) != mode):
        raise RuntimeFailure("A private daemon directory did not retain its exact credentials and mode")
    return path


def file_owner(path: Path, account: dict, *, mode=0o640, fifo=False):
    info = path.lstat()
    expected = stat.S_ISFIFO(info.st_mode) if fifo else stat.S_ISREG(info.st_mode)
    if (not expected or (not fifo and info.st_nlink != 1)
            or info.st_uid not in {0, account["uid"]}):
        raise RuntimeFailure("A generated daemon file changed ownership or type")
    os.chown(path, 0 if not fifo else account["uid"], account["gid"], follow_symlinks=False)
    path.chmod(mode)


def prepare_discovery(directory_path: Path, account: dict, clients: list[dict], record: dict):
    directory(directory_path)
    for name, mode in [("bus", 0o755), ("avahi", 0o700)]:
        directory(directory_path / name, account, mode=mode)
    directory(directory_path / "config")
    policies = []
    allowed = {item["name"] for item in clients} | {account["name"]}
    allow_users = "".join(f'<allow user="{escape(name)}"/>' for name in sorted(allowed))
    for name in sorted(allowed):
        own = '<allow own="org.freedesktop.Avahi"/>' if name == account["name"] else ""
        route = ("*" if name == account["name"] else "org.freedesktop.Avahi")
        policies.append(
            f'<policy user="{escape(name)}">{own}'
            f'<allow send_destination="{route}"/>'
            f'<allow receive_sender="{route}"/>'
            '<allow send_destination="org.freedesktop.DBus"/>'
            '<allow receive_sender="org.freedesktop.DBus"/></policy>'
        )
    dbus = directory_path / "config" / "dbus.conf"
    dbus.write_text(
        '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN" '
        '"http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
        '<busconfig><type>system</type><auth>EXTERNAL</auth>'
        f'<listen>unix:path={VIEW}/bus/bus.sock</listen>'
        '<policy context="default"><deny user="*"/>' + allow_users +
        '<deny own="*"/><deny send_destination="*"/><deny receive_sender="*"/>'
        '</policy>' + ''.join(policies) + '</busconfig>\n', encoding="utf-8",
    )
    avahi = directory_path / "config" / "avahi.conf"
    avahi.write_text(
        '[server]\n' + f'host-name={record["namespace"].replace("_", "-")[:63]}\n'
        'use-ipv4=yes\nuse-ipv6=no\n' + f'allow-interfaces={record["interface"]}\n'
        'ratelimit-interval-usec=1000000\nratelimit-burst=1000\n'
        '[publish]\ndisable-publishing=no\npublish-addresses=yes\n'
        'publish-hinfo=no\npublish-workstation=no\n'
        '[reflector]\nenable-reflector=no\n', encoding="utf-8",
    )
    for config in [dbus, avahi]:
        file_owner(config, account)
    return (
        Bind(str(directory_path / "bus"), str(VIEW / "bus"), True),
        Bind(str(directory_path / "avahi"), "/run/avahi-daemon", True),
    )


def prepare_room_view(path: Path, audio: dict, receiver: dict, output: dict):
    # backend_configs prepared root-owned pipes/config/metadata first. Writable
    # output database/cache belongs only to its output user; queues stay raw.
    directory(path)
    directory(path / "output", output)
    directory(path / "output" / "cache", output)
    # OwnTone publishes one credential-checked datagram endpoint. The audio
    # user may traverse and send to it, but cannot replace its directory entry.
    directory(path / "overlay", {**output, "gid": audio["gid"]}, mode=0o2710)
    directory(path / "control", audio)
    directory(path / "input", {**audio, "gid": receiver["gid"]}, mode=0o2750)
    directory(path / "credentials")
    directory(path / "pipes", mode=0o755)
    directory(path / "metadata", receiver)
    file_owner(path / "metadata" / "shairport.pipe", receiver, mode=0o600, fifo=True)
    pipe = path / "pipes" / "audio.pipe"
    file_owner(pipe, audio, mode=0o660, fifo=True)
    os.chown(pipe, audio["uid"], output["gid"], follow_symlinks=False)
    file_owner(path / "config" / "shairport.conf", receiver)
    file_owner(path / "config" / "owntone.conf", output)


def audio_devices(device: str | None):
    """Resolve a validated named PCM to exact playback nodes, without groups.

    ALSA's card control node is intentionally omitted: software volume needs no
    card-wide hardware mixer authority. Linux integration must prove PCM opens.
    """
    if not device:
        return ()
    import re
    match = re.fullmatch(r"(?:plug)?hw:CARD=([A-Za-z0-9_-]+),DEV=(\d+),SUBDEV=(\d+)", device)
    if not match or match[1].isdecimal():
        raise RuntimeFailure("Local output needs a validated stable named ALSA playback device")
    cards = []
    for identity in Path("/proc/asound").glob("card[0-9]*/id"):
        if identity.read_text().strip() == match[1]:
            cards.append(identity.parent.name.removeprefix("card"))
    if len(cards) != 1:
        raise RuntimeFailure("The configured named ALSA card is missing or ambiguous")
    node = Path(f"/dev/snd/pcmC{cards[0]}D{match[2]}p")
    info = node.lstat()
    if not stat.S_ISCHR(info.st_mode):
        raise RuntimeFailure("The configured playback device is not an ALSA character device")
    return (str(node),)


def admit_bus_socket(path: Path, account: dict):
    """Set IPC permissions through an inode handle, never a writable pathname.

    The discovery user writes this directory. O_PATH+NOFOLLOW binds the checked
    socket inode; /proc/self/fd then changes that inode even if a daemon swaps
    its filename, so no symlink can redirect a root chmod into host files.
    """
    descriptor = os.open(path, os.O_PATH | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != account["uid"]:
            raise RuntimeFailure("Private discovery socket ownership changed")
        os.chmod(f"/proc/self/fd/{descriptor}", 0o666)
    finally:
        os.close(descriptor)
