"""Download every GGUF in the bake-off roster.

Usage:  uv run python model/download.py [--only ID] [--dest model/gguf]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", default="model/models.yaml")
    ap.add_argument("--dest", default="model/gguf")
    ap.add_argument("--only", default=None, help="download a single model id")
    args = ap.parse_args()

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("huggingface_hub missing: uv add --dev huggingface_hub", file=sys.stderr)
        return 1

    roster = yaml.safe_load(pathlib.Path(args.roster).read_text())
    dest = pathlib.Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    for m in roster["models"]:
        if args.only and m["id"] != args.only:
            continue
        target = dest / f"{m['id']}.gguf"
        if target.is_symlink() or target.exists():
            if target.exists():
                size = target.stat().st_size / 1e9
                print(f"{m['id']:22} present ({size:.2f} GB)")
                continue
            # Dangling symlink from an interrupted run: re-point it below.
            target.unlink()
        print(f"{m['id']:22} downloading {m['repo']}/{m['file']} ...")
        try:
            path = hf_hub_download(repo_id=m["repo"], filename=m["file"])
            target.unlink(missing_ok=True)
            target.symlink_to(path)
            print(f"{m['id']:22} ok -> {path}")
        except Exception as exc:  # noqa: BLE001 - report and continue the roster
            print(f"{m['id']:22} FAILED: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
