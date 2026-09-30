"""Minimal backend configuration with one explicit 48 kHz stereo contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import stat
import sys
from xml.sax.saxutils import escape

from .system import RuntimeFailure


def quote(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def write_private(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def prepare_room(directory: Path):
    for child in [
        directory,
        directory / "pipes",
        directory / "metadata",
        directory / "config",
        directory / "logs",
        directory / "cache",
    ]:
        child.mkdir(parents=True, exist_ok=True, mode=0o700)
        child.chmod(0o700)
    # Only audio goes in OwnTone's watched library. Keep the Shairport metadata
    # endpoint private for timing inspection; it has a bounded write timeout.
    for obsolete in [
        directory / "pipes" / "audio.pipe.metadata",
        directory / "pipes" / "shairport.metadata",
    ]:
        try:
            info = obsolete.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISFIFO(info.st_mode) and info.st_uid == os.geteuid():
            obsolete.unlink()
    for pipe in [directory / "pipes" / "audio.pipe", directory / "metadata" / "shairport.pipe"]:
        if pipe.exists():
            if not stat.S_ISFIFO(pipe.stat().st_mode):
                raise RuntimeFailure(f"Refusing to replace a non-FIFO room pipe: {pipe}")
        else:
            os.mkfifo(pipe, 0o600)


def isolated_runtime(directory: Path, namespace: str, interface: str):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ["shm", "avahi"]:
        (directory / name).mkdir(exist_ok=True, mode=0o700)
    socket = directory / "bus.sock"
    config = directory / "dbus.conf"
    write_private(
        config,
        f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>system</type><listen>unix:path={escape(str(socket))}</listen>
  <policy user="root"><allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/></policy>
</busconfig>
""",
    )
    avahi = directory / "avahi.conf"
    write_private(
        avahi,
        f"""[server]
host-name={namespace.replace("_", "-")[:63]}
use-ipv4=yes
use-ipv6=no
allow-interfaces={interface}
ratelimit-interval-usec=1000000
ratelimit-burst=1000
[publish]
disable-publishing=no
publish-addresses=yes
publish-hinfo=no
publish-workstation=no
[reflector]
enable-reflector=no
""",
    )
    return config, avahi, socket


def isolated_command(directory: Path, namespace: str, command: list[str]) -> list[str]:
    socket = directory / "bus.sock"
    environment = ["env", f"DBUS_SYSTEM_BUS_ADDRESS=unix:path={socket}"] + command
    script = (
        f"mount --bind {shlex.quote(str(directory / 'shm'))} /dev/shm && "
        f"mount --bind {shlex.quote(str(directory / 'avahi'))} /run/avahi-daemon && "
        f"exec ip netns exec {shlex.quote(namespace)} {shlex.join(environment)}"
    )
    return ["unshare", "--mount", "--propagation", "private", "/bin/sh", "-c", script]


def backend_configs(
    room,
    directory: Path,
    receiver: dict,
    sender: dict,
    *,
    broker_socket: Path,
    all_receiver_names: list[str],
    password: str,
) -> tuple[Path, Path]:
    prepare_room(directory)
    shairport = directory / "config" / "shairport.conf"
    phone_volume = (
        shlex.join(
            [
                sys.executable,
                "-m",
                "shiri.runtime.volume",
                "--socket",
                str(broker_socket),
                "--room-id",
                room.id,
            ]
        )
        + " "
    )
    music_hook = [
        sys.executable,
        "-m",
        "shiri.runtime.audio",
        "--socket",
        str(directory / "audio.sock"),
        "--signal",
    ]
    write_private(
        shairport,
        f"""general = {{
  name = {quote(room.airplay_name)};
  service_type = "airplay2";
  interface = {quote(receiver["interface"])};
  port = 7000;
  output_backend = "alsa";
  mdns_backend = "avahi";
  interpolation = "soxr";
  ignore_volume_control = "yes";
  run_this_when_volume_is_set = {quote(phone_volume)};
  audio_backend_buffer_desired_length_in_seconds = 0.15;
  audio_backend_latency_offset_in_seconds = 0.0;
}};
sessioncontrol = {{
  run_this_before_play_begins = {quote(shlex.join(music_hook + ["music-start"]))};
  run_this_after_play_ends = {quote(shlex.join(music_hook + ["music-stop"]))};
  wait_for_completion = "yes";
}};
alsa = {{
  output_device = {quote(f"hw:Loopback,0,{room.slot}")};
  output_rate = 48000;
  output_format = "S16_LE";
  output_channels = 2;
  use_precision_timing = "no";
}};
metadata = {{
  enabled = "yes";
  include_cover_art = "no";
  pipe_name = {quote(directory / "metadata" / "shairport.pipe")};
  pipe_timeout = 100;
}};
""",
    )
    owntone = directory / "config" / "owntone.conf"
    exclusions = "\n".join(f"airplay {quote(name)} {{ exclude = true }}" for name in all_receiver_names)
    local_device = (
        f"hw:Loopback,1,{room.slot}"
        if room.local_audio_device and room.local_audio_device.lower().startswith("bluealsa")
        else room.local_audio_device
    )
    local_audio = (
        f'type = "alsa"\n card = {quote(local_device)}\n software_volume = true\n'
        f' nickname = {quote(room.name + " local speaker")}'
        if room.local_audio_device
        else 'type = "disabled"'
    )
    write_private(
        owntone,
        f"""general {{
  uid = "root"
  db_path = {quote(directory / "songs.db")}
  logfile = "/dev/stdout"
  loglevel = log
  cache_dir = {quote(directory / "cache")}
  admin_password = {quote(password)}
  trusted_networks = {{ {quote(sender["api_host_ip"] + "/32")}, {quote(sender["api_ip"] + "/32")} }}
  websocket_port = 0
  ipv6 = no
  speaker_autoselect = no
  high_resolution_clock = yes
  start_buffer_ms = 500
}}
library {{
  name = {quote(room.airplay_name + " output")}
  port = {3869 + room.slot * 10}
  directories = {{ {quote(directory / "pipes")} }}
  follow_symlinks = false
  pipe_autostart = true
  pipe_sample_rate = 48000
  pipe_bits_per_sample = 16
}}
audio {{ {local_audio} }}
mpd {{
  port = 0
  http_port = 0
}}
streaming {{ sample_rate = 48000 bit_rate = 192 }}
{exclusions}
""",
    )
    return shairport, owntone
