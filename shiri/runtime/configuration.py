"""Minimal backend configuration with one explicit 48 kHz stereo contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
from uuid import UUID
from xml.sax.saxutils import escape

from .system import RuntimeFailure
from .latency import (
    MINIMUM_LOCAL_OUTPUT_BUFFER_MS, minimum_room_buffer_ms, minimum_speaker_lead_ms, room_buffer_ms, speaker_lead_ms,
)


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
    view_directory: Path | None = None,
    output_state_directory: Path | None = None,
    own_username: str = "root",
    native_timing: bool = False,
    audio_uid: int | None = None,
    music_socket: Path | None = None,
    pcm_identity_file: Path | None = None,
    framed_output: dict | None = None,
    speech_output: dict | None = None,
    output_buffer_ms: int | None = None,
    minimum_latency: bool | None = None,
) -> tuple[Path, Path]:
    view = view_directory or directory
    if minimum_latency is None:
        minimum_latency = native_timing is True
    if type(minimum_latency) is not bool or minimum_latency and native_timing is not True:
        raise RuntimeFailure("Minimum buffering requires an explicit native candidate profile")
    lead_policy = minimum_speaker_lead_ms if minimum_latency else speaker_lead_ms
    if output_buffer_ms is None:
        # Native rooms use the route plan. The explicit legacy PCM profile
        # retains its original protocol lead; it cannot silently admit B40.
        output_buffer_ms = minimum_room_buffer_ms(room) if minimum_latency else max(
            500, room_buffer_ms(room),
            max((speaker_lead_ms(speaker) - speaker.offset_ms for speaker in room.speakers), default=0),
        )
    minimum_buffer = MINIMUM_LOCAL_OUTPUT_BUFFER_MS if minimum_latency else 251
    if type(output_buffer_ms) is not int or not minimum_buffer <= output_buffer_ms <= 4250:
        raise RuntimeFailure("OwnTone output buffering requires a bounded protocol lead")
    # The broker has already admitted the hardware device before replacing it
    # with the private mapped PCM name. Validate the selected speaker leads
    # here without reinterpreting that internal name as a public device.
    if any(output_buffer_ms < lead_policy(speaker) - (min(0, speaker.offset_ms) if minimum_latency else speaker.offset_ms)
           for speaker in room.speakers):
        raise RuntimeFailure("OwnTone output buffering cannot retain a selected speaker's required timing lead")
    speech_settings = ""
    if speech_output is not None:
        if (type(speech_output) is not dict
                or set(speech_output) != {"socket", "peer_uid", "room_id", "launch_generation"}
                or not native_timing or own_username == "root"
                or speech_output["socket"] != view / "overlay" / "speech.sock"
                or len(os.fsencode(speech_output["socket"])) >= 108
                or type(speech_output["peer_uid"]) is not int or not 0 < speech_output["peer_uid"] < 2**32
                or speech_output["peer_uid"] != audio_uid
                or speech_output["room_id"] != room.id):
            raise RuntimeFailure("Speech output requires this exact private native room profile")
        try:
            launch = speech_output["launch_generation"]
            valid_launch = UUID(launch).int != 0 and UUID(launch).hex == launch
        except (ValueError, AttributeError, TypeError):
            valid_launch = False
        if not valid_launch:
            raise RuntimeFailure("Speech output requires the exact nonempty room launch generation")
        speech_settings = (
            f'  shiri_speech_socket = {quote(speech_output["socket"])}\n'
            f'  shiri_speech_peer_uid = {speech_output["peer_uid"]}\n'
            f'  shiri_speech_room_id = {quote(room.id)}\n'
            f'  shiri_speech_launch_generation = {quote(launch)}\n'
        )
    if framed_output is not None:
        expected_keys = {"socket", "peer_uid", "room_id", "launch_generation", "rate", "channels", "format_code"}
        if (type(framed_output) is not dict or set(framed_output) != expected_keys
                or not native_timing or own_username == "root" or pcm_identity_file is not None
                or not isinstance(room.local_audio_device, str)
                or not room.local_audio_device.lower().startswith("bluealsa:")):
            raise RuntimeFailure("Framed Bluetooth output requires one exact isolated admitted output profile")
        socket = framed_output["socket"]
        if (not isinstance(socket, Path) or not socket.is_absolute()
                or socket != view / "bridge" / "final-pcm.sock" or len(os.fsencode(socket)) >= 108):
            raise RuntimeFailure("Framed Bluetooth output requires the exact private final PCM socket")
        for key, minimum, maximum in [("peer_uid", 1, 2**32 - 1), ("rate", 8000, 192000), ("channels", 1, 2)]:
            value = framed_output[key]
            if type(value) is not int or not minimum <= value <= maximum:
                raise RuntimeFailure("Framed Bluetooth output has invalid admitted PCM capabilities or peer UID")
        if type(framed_output["format_code"]) is not int or framed_output["format_code"] not in {0x8210, 0x8318, 0x8418, 0x8420}:
            raise RuntimeFailure("Framed Bluetooth output requires exact signed little-endian PCM capabilities")
        identifier, generation = framed_output["room_id"], framed_output["launch_generation"]
        try:
            exact_room = isinstance(identifier, str) and str(UUID(identifier)) == identifier == room.id and UUID(identifier).int != 0
        except (ValueError, TypeError, AttributeError):
            exact_room = False
        if (not exact_room or not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None
                or generation == "0" * 32):
            raise RuntimeFailure("Framed Bluetooth output must belong to this exact room and nonempty launch generation")
    prepare_room(directory)
    own_state = output_state_directory or view
    if native_timing and (type(audio_uid) is not int or audio_uid < 0):
        raise RuntimeFailure("Native timing requires the exact private audio worker UID")
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
    volume_hook = "" if native_timing else f"  run_this_when_volume_is_set = {quote(phone_volume)};\n"
    session_controls = "" if native_timing else f"""sessioncontrol = {{
  run_this_before_play_begins = {quote(shlex.join(music_hook + ["music-start"]))};
  run_this_after_play_ends = {quote(shlex.join(music_hook + ["music-stop"]))};
  wait_for_completion = "yes";
}};
"""
    receiver_settings = (f"""shiri = {{
  socket = {quote(music_socket or view / "input" / "music.sock")};
  peer_uid = {audio_uid};
  output_rate = 48000;
  output_format = "S16_LE";
  output_channels = 2;
}};
""" if native_timing else f"""alsa = {{
  output_device = {quote(f"hw:Loopback,0,{room.slot}")};
  output_rate = 48000;
  output_format = "S16_LE";
  output_channels = 2;
  use_precision_timing = "no";
}};
""")
    write_private(
        shairport,
        f"""general = {{
  name = {quote(room.airplay_name)};
  service_type = "airplay2";
  interface = {quote(receiver["interface"])};
  port = 7000;
  output_backend = {quote("shiri" if native_timing else "alsa")};
  mdns_backend = "avahi";
  interpolation = "soxr";
  ignore_volume_control = "yes";
  default_airplay_volume = {room.volume * 0.3 - 30:.6f};
{volume_hook}  audio_backend_buffer_desired_length_in_seconds = 0.15;
  audio_backend_latency_offset_in_seconds = 0.0;
}};
{session_controls}{receiver_settings}metadata = {{
  enabled = "yes";
  include_cover_art = "no";
  pipe_name = {quote(view / "metadata" / "shairport.pipe")};
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
    if framed_output is not None:
        local_audio = (
            'type = "shiri-pcm"\n software_volume = true\n'
            f' nickname = {quote(room.name + " local speaker")}\n'
            f' shiri_pcm_socket = {quote(framed_output["socket"])}\n'
            f' shiri_pcm_peer_uid = {framed_output["peer_uid"]}\n'
            f' shiri_pcm_room_id = {quote(framed_output["room_id"])}\n'
            f' shiri_pcm_launch_generation = {quote(framed_output["launch_generation"])}\n'
            f' shiri_pcm_rate = {framed_output["rate"]}\n'
            f' shiri_pcm_channels = {framed_output["channels"]}\n'
            f' shiri_pcm_format = {framed_output["format_code"]}'
        )
    if pcm_identity_file is not None:
        if room.local_audio_device is None:
            raise RuntimeFailure("A PCM identity pin requires an explicit local output device")
        local_audio += f'\n pcm_identity_file = {quote(pcm_identity_file)}'
    trusted = "" if native_timing or own_username != "root" else f'{quote(sender["api_host_ip"] + "/32")}, {quote(sender["api_ip"] + "/32")}'
    framed = "  pipe_framed = true\n" if native_timing else ""
    write_private(
        owntone,
        f"""general {{
  uid = {quote(own_username)}
  db_path = {quote(own_state / "songs.db")}
  logfile = "/dev/null"
  loglevel = log
  cache_dir = {quote(own_state / "cache")}
  admin_password = {quote(password)}
  trusted_networks = {{ {trusted} }}
  websocket_port = 0
  ipv6 = no
  speaker_autoselect = no
  high_resolution_clock = yes
  start_buffer_ms = {output_buffer_ms}
{speech_settings}}}
library {{
  name = {quote(room.airplay_name + " output")}
  port = {3869 + room.slot * 10}
  directories = {{ {quote(view / "pipes")} }}
  follow_symlinks = false
  pipe_autostart = true
  pipe_sample_rate = 48000
  pipe_bits_per_sample = 16
{framed}}}
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
