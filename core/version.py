"""
Version stamps: which code, which strategy source and which parameters produced a result.

  code_version    the git commit the running code was deployed from, e.g. "cdf9f22" or "cdf9f22-dirty"
                  (uncommitted changes at deploy time). The Raspberry Pi has no git, so the hash is written
                  into a VERSION file by deploy/stamp.py on the PC at deploy time and copied with the code.
                  Without a VERSION file a local git checkout is asked directly; otherwise "unknown".
  source_hash     sha256 of the strategy's own source file (line endings normalised, so the PC's CRLF copy
                  and the Pi's LF copy hash the same). Verifiable without git.
  params_hash     sha256 of the strategy's RESOLVED parameters (defaults + config.yaml overrides) as sorted
                  JSON, numbers normalised (25 and 25.0 hash the same).
  config_version  sha256 of the parsed config.yaml (comments and formatting don't change it).

All hashes are the first 12 hex characters. Rows written before stamping existed have NULL stamps and are
treated as "unversioned".
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
from typing import Optional

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(HERE, "VERSION")
HASH_LEN = 12
UNVERSIONED = "unversioned"


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:HASH_LEN]


def _norm(x):
    """Numbers as floats (25 == 25.0), keys as strings, containers recursively; bools stay bools."""
    if isinstance(x, bool) or x is None or isinstance(x, str):
        return x
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, dict):
        return {str(k): _norm(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_norm(v) for v in (sorted(x, key=str) if isinstance(x, set) else x)]
    return str(x)


def stable_hash(obj) -> str:
    """Hash of any JSON-like object, independent of key order and of int-vs-float spelling."""
    return _h(json.dumps(_norm(obj), sort_keys=True, separators=(",", ":")).encode("utf-8"))


def params_hash(params: dict) -> str:
    return stable_hash(params or {})


def file_hash(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return _h(fh.read().replace(b"\r\n", b"\n"))
    except OSError:
        return None


def source_hash(obj) -> Optional[str]:
    """
    Hash of the source file that defines a strategy (pass the instance or its class). A strategy that subclasses
    another strategy (e.g. a control built on the Sniper) also covers its parents' files, so a change in the parent
    shows up too. The shared base class (strategies/base.py) is not part of it: that is what code_version is for.
    """
    cls = obj if inspect.isclass(obj) else type(obj)
    hashes = []
    for c in cls.__mro__:
        if c is object or (c.__module__.endswith("strategies.base") and c.__name__ == "Strategy"):
            continue
        try:
            path = inspect.getsourcefile(c)
        except TypeError:
            continue
        h = file_hash(path) if path else None
        if h and h not in hashes:
            hashes.append(h)
    if not hashes:
        return None
    return hashes[0] if len(hashes) == 1 else _h("+".join(hashes).encode())


def read_version_file(path: str = VERSION_FILE) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) and d.get("commit") else None
    except (OSError, ValueError):
        return None


def git_version(repo: str = HERE) -> Optional[dict]:
    """{'commit', 'dirty'} from a local git checkout (None when git or the repo isn't there, e.g. on the Pi)."""
    if not os.path.isdir(os.path.join(repo, ".git")):
        return None
    try:
        run = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, timeout=10)
        head = run("rev-parse", "HEAD")
        if head.returncode != 0:
            return None
        dirty = bool(run("status", "--porcelain").stdout.strip())
        return {"commit": head.stdout.strip(), "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return None


def format_version(d: Optional[dict]) -> str:
    if not d or not d.get("commit"):
        return "unknown"
    return str(d["commit"])[:HASH_LEN] + ("-dirty" if d.get("dirty") else "")


_cached: Optional[str] = None


def code_version(refresh: bool = False) -> str:
    """The deployed code version: VERSION file first (the Pi), else the local git checkout, else 'unknown'."""
    global _cached
    if _cached is None or refresh:
        _cached = format_version(read_version_file() or git_version())
    return _cached


def strategy_stamp(strategy, config_version: Optional[str] = None) -> dict:
    """The stamp recorded with a strategy's registration, fills and settlements."""
    return {"code_version": code_version(), "params_hash": params_hash(getattr(strategy, "p", {})),
            "source_hash": source_hash(strategy), "config_version": config_version}
