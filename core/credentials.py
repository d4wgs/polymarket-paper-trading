"""
Where API credentials come from. They are NEVER written in config.yaml or any tracked file.

Lookup order:
  1. the environment variable          POLYMARKET_US_API_KEY   (and POLYMARKET_US_API_SECRET if the exchange issues one)
  2. a gitignored file next to run.py  .env   (lines like  POLYMARKET_US_API_KEY=abc123 )

On the Raspberry Pi put it in ~/polymarket-paper/.env and run `chmod 600 .env`.
`.env` is listed in .gitignore, so `git add .` can't commit it. See .env.example.
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(HERE, ".env")


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
    """Environment variable first, then the gitignored .env file."""
    return os.environ.get(name) or _read_env_file().get(name) or default


def api_key() -> str | None:
    return get_secret("POLYMARKET_US_API_KEY")
