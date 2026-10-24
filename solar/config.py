"""Local configuration. Never put installation-specific values in source control."""

import base64
import functools
import ipaddress
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tomllib

import psutil
import tomlkit


ROOT = Path(os.environ.get("SOLAR_HOME", Path.cwd())).resolve()
CONFIG_PATH = Path(os.environ.get("SOLAR_CONFIG", ROOT / "solar.conf")).resolve()

DEFAULTS = {
    "server": {
        "hosts": ["0.0.0.0", "::"],
        "port": 5557,
        "allowed_hosts": [],
        "tls": True,
        "certificate": "",
        "private_key": "",
    },
    "storage": {"directory": ".solar", "max_file_mb": 50, "quota_mb": 2048, "export_mb": 250},
    "execution": {"max_workers": 3, "timeout_seconds": 3600, "native_idle_seconds": 1800},
    "desktop": {"enabled": True, "fps": 4, "max_width": 1600, "idle_seconds": 300, "max_minutes": 60},
    "providers": {"allowed_api_origins": ["http://127.0.0.1:11434", "http://127.0.0.1:1234", "https://api.openai.com"]},
    "bootstrap": {},
}


def private_permissions(path: Path, directory=False):
    """Protect runtime secrets from other local OS accounts, including inherited Windows ACLs."""
    if os.name == "nt":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
        import csv
        sid = next(csv.reader([result.stdout.strip()]))[1]
        grant = f"*{sid}:(OI)(CI)F" if directory else f"*{sid}:F"
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant, "*S-1-5-18:F"],
            capture_output=True, check=True,
        )
    else:
        path.chmod(0o700 if directory else 0o600)


def local_hosts():
    hosts = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower()}
    for entries in psutil.net_if_addrs().values():
        for addr in entries:
            if addr.family in (socket.AF_INET, socket.AF_INET6):
                hosts.add(addr.address.split("%")[0])
    return sorted(hosts)


@functools.lru_cache(maxsize=1)
def config():
    settings = {section: values.copy() for section, values in DEFAULTS.items()}
    if CONFIG_PATH.exists():
        loaded = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for section, values in loaded.items():
            if section in settings:
                settings[section].update(values)
    storage = Path(settings["storage"]["directory"])
    if not storage.is_absolute():
        storage = ROOT / storage
    settings["data_dir"] = storage.resolve()
    server = settings["server"]
    if type(server["port"]) is not int or not 1 <= server["port"] <= 65535:
        raise ValueError("server.port must be between 1 and 65535")
    if not isinstance(server["hosts"], list) or not server["hosts"] or type(server["tls"]) is not bool:
        raise ValueError("server.hosts must contain at least one IP address")
    for host in server["hosts"]:
        address = ipaddress.ip_address(host)
        if not server["tls"] and not address.is_loopback:
            raise ValueError("TLS is required when listening outside loopback")
    if any(h == "*" for h in server["allowed_hosts"]):
        raise ValueError("Use explicit allowed_hosts, not a wildcard")
    for section, field, low, high in (
        ("storage", "max_file_mb", 1, 100), ("storage", "quota_mb", 1, 102400),
        ("storage", "export_mb", 1, 500), ("execution", "max_workers", 1, 16),
        ("execution", "timeout_seconds", 10, 864000), ("execution", "native_idle_seconds", 30, 86400),
        ("desktop", "fps", 1, 10), ("desktop", "max_width", 320, 4096),
        ("desktop", "idle_seconds", 15, 3600), ("desktop", "max_minutes", 1, 720),
    ):
        value = settings[section][field]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{section}.{field} must be an integer between {low} and {high}")
    return settings


def save_config(settings):
    serializable = {k: v for k, v in settings.items() if k in DEFAULTS}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".conf.tmp")
    temporary.write_text(
        "# Solar local configuration. Contains encrypted bootstrap credentials; NEVER commit.\n"
        "# Change hosts / port below, then restart: uv run solar\n" + tomlkit.dumps(serializable),
        encoding="utf-8",
    )
    private_permissions(temporary)
    temporary.replace(CONFIG_PATH)


def initialize_storage():
    settings = config()
    directory = settings["data_dir"]
    directory.mkdir(parents=True, exist_ok=True)
    private_permissions(directory, directory=True)
    key_path = directory / "master.key"
    if not key_path.exists():
        # Losing a key while keeping the database must never silently create a new key.
        if (directory / "solar.sqlite3").exists():
            raise RuntimeError("Master key missing. Restore master.key from your backup before starting Solar.")
        with key_path.open("xb") as handle:
            handle.write(base64.urlsafe_b64encode(secrets.token_bytes(32)))
        private_permissions(key_path)
    for name in ("chats", "runtime", "locks", "tls"):
        (directory / name).mkdir(exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(settings)
    return settings
