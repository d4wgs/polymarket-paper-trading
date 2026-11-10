"""
Write the VERSION file that tells the bot which git commit it was deployed from.

Run it on the PC (where git is) right before copying the code to the Raspberry Pi, then copy VERSION along
with the changed files:

    python deploy/stamp.py            # writes VERSION next to run.py and prints it
    python deploy/stamp.py --check    # print what would be written, change nothing

VERSION is a small JSON file: {"commit": full hash, "dirty": true/false, "branch", "stamped_at" (UTC)}.
"dirty" means there were uncommitted changes when it was stamped: results stamped "<hash>-dirty" can't be tied
to an exact commit, so commit first whenever possible. VERSION holds no secrets and is gitignored (a commit
can't contain its own hash).
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from core.version import VERSION_FILE, format_version, git_version   # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="print only; don't write the file")
    ap.add_argument("--out", default=VERSION_FILE)
    args = ap.parse_args(argv)
    v = git_version(HERE)
    if v is None:
        print("deploy/stamp.py: this folder is not a git checkout (or git is not installed); nothing written.",
              file=sys.stderr)
        return 1
    try:
        branch = subprocess.run(["git", "-C", HERE, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        branch = ""
    v.update(branch=branch, stamped_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    print(f"code version: {format_version(v)}  (branch {branch or '?'}, stamped {v['stamped_at']})")
    if v["dirty"]:
        print("WARNING: uncommitted changes -> the stamp is marked -dirty. Commit first for an exact version.")
    if not args.check:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(v, fh, indent=1)
            fh.write("\n")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
