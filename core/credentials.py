"""
Where API credentials come from. They are NEVER written in config.yaml or any tracked file.

Lookup order for each name:
  1. the environment variable        (systemd's EnvironmentFile puts them here on the Pi)
  2. a gitignored file next to run.py named  .env   (lines like  PM_KEY_ID=...  )

  PM_KEY_ID       the API Key ID from https://polymarket.us/developer
  PM_SECRET_KEY   the base64 Secret Key (shown once)

On the Pi keep the file at ~/polymarket-paper/.env with `chmod 600`. `.env` is in .gitignore.
Nothing here ever prints or logs a value.
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(HERE, ".env")
_ALIASES = {"PM_KEY_ID": ("POLYMARKET_KEY_ID",), "PM_SECRET_KEY": ("POLYMARKET_SECRET_KEY",)}


def _read_env_file(path: str = ENV_FILE) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def get_secret(name: str, default: str | None = None) -> str | None:
    """Environment variable first, then the gitignored .env file (also trying the SDK's alias names)."""
    file_vals = _read_env_file()
    for n in (name, *_ALIASES.get(name, ())):
        v = os.environ.get(n) or file_vals.get(n)
        if v:
            return v
    return default


def pm_credentials() -> tuple[str | None, str | None]:
    """(key id, secret key) for Polymarket US; either may be None if not configured."""
    return get_secret("PM_KEY_ID"), get_secret("PM_SECRET_KEY")
