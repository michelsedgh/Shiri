"""Explicit installation settings shared by the API and privileged runtime."""
from dataclasses import dataclass
import os
import ipaddress
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    state_dir: Path = Path("/var/lib/shiri")
    runtime_dir: Path = Path("/run/shiri")
    runtime_state_dir: Path = Path("/var/lib/shiri-runtime")
    runtime_socket: Path = Path("/run/shiri/runtime.sock")
    binary_dir: Path | None = None
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    simulation: bool = False
    allow_unauthenticated: bool = False
    api_token_file: Path = Path("/etc/shiri/api-token")
    daemon_identity_file: Path = Path("/etc/shiri/daemon-identities.json")
    bind_policy_helper: Path | None = None
    pcm_exec_helper: Path | None = None
    max_rooms: int = 8
    trusted_proxy_ips: str = "127.0.0.1,::1"
    tts_worker_url: str | None = None
    tts_worker_token_file: Path | None = None
    speaker_readiness: str = "ready"

    def __post_init__(self):
        if type(self.speaker_readiness) is not str or self.speaker_readiness not in {"adaptive", "ready", "on_demand"}:
            raise ValueError("Speaker readiness must be adaptive, ready or on_demand")
        if type(self.allow_unauthenticated) is not bool:
            raise ValueError("Unauthenticated access must be explicitly enabled with a boolean")
        if type(self.api_port) is not int or not 1 <= self.api_port <= 65535:
            raise ValueError("API port must be between 1 and 65535")
        if type(self.max_rooms) is not int or not 1 <= self.max_rooms <= 8:
            raise ValueError("Room capacity must be between 1 and 8")
        for address in self.trusted_proxy_ips.split(","):
            if address:
                ipaddress.ip_network(address.strip(), strict=False)
        if not self.api_host or any(ord(char) < 32 for char in self.api_host):
            raise ValueError("API host must be a nonempty address")
        if self.tts_worker_url:
            from urllib.parse import urlsplit
            if any(ord(char) <= 32 or ord(char) == 127 for char in self.tts_worker_url):
                raise ValueError("TTS worker origin must not contain whitespace or control characters")
            address = urlsplit(self.tts_worker_url)
            _ = address.port  # Validate malformed and out-of-range explicit ports.
            if address.scheme not in {"http", "https"} or not address.hostname or address.username or address.password or address.query or address.fragment or address.path not in {"", "/"}:
                raise ValueError("TTS worker must be a trusted installation HTTP(S) origin")
            if self.tts_worker_token_file is None:
                raise ValueError("Configure a private TTS worker token file")

    @property
    def database(self) -> Path:
        return self.state_dir / "shiri.sqlite3"

    @classmethod
    def from_env(cls):
        runtime_dir = Path(os.environ.get("SHIRI_RUNTIME_DIR", "/run/shiri"))
        return cls(
            state_dir=Path(os.environ.get("SHIRI_STATE_DIR", "/var/lib/shiri")),
            runtime_dir=runtime_dir,
            runtime_state_dir=Path(os.environ.get("SHIRI_RUNTIME_STATE_DIR", "/var/lib/shiri-runtime")),
            runtime_socket=Path(os.environ.get("SHIRI_RUNTIME_SOCKET", str(runtime_dir / "runtime.sock"))),
            binary_dir=Path(os.environ["SHIRI_BINARY_DIR"]) if os.environ.get("SHIRI_BINARY_DIR") else None,
            api_host=os.environ.get("SHIRI_HOST", "127.0.0.1"),
            api_port=int(os.environ.get("SHIRI_PORT", "8080")),
            simulation=os.environ.get("SHIRI_SIMULATION", "0") == "1",
            allow_unauthenticated=os.environ.get("SHIRI_ALLOW_UNAUTHENTICATED", "0") == "1",
            api_token_file=Path(os.environ.get("SHIRI_API_TOKEN_FILE", "/etc/shiri/api-token")),
            daemon_identity_file=Path(os.environ.get("SHIRI_DAEMON_IDENTITY_FILE", "/etc/shiri/daemon-identities.json")),
            bind_policy_helper=(Path(os.environ["SHIRI_BIND_POLICY_HELPER"])
                                if os.environ.get("SHIRI_BIND_POLICY_HELPER") else None),
            pcm_exec_helper=(Path(os.environ["SHIRI_PCM_EXEC_HELPER"])
                             if os.environ.get("SHIRI_PCM_EXEC_HELPER") else None),
            max_rooms=int(os.environ.get("SHIRI_MAX_ROOMS", "8")),
            trusted_proxy_ips=os.environ.get("SHIRI_TRUSTED_PROXY_IPS", "127.0.0.1,::1"),
            tts_worker_url=os.environ.get("SHIRI_TTS_WORKER_URL") or None,
            tts_worker_token_file=(Path(os.environ["SHIRI_TTS_WORKER_TOKEN_FILE"])
                                   if os.environ.get("SHIRI_TTS_WORKER_TOKEN_FILE") else None),
            speaker_readiness=os.environ.get("SHIRI_SPEAKER_READINESS", "ready"),
        )


RuntimeConfig = Settings
