#!/usr/bin/env python3
"""Parse every PDF under the given folders, each in its own process, and count
the outcomes. The requirement is zero crashes, zero accounting failures and
zero timeouts. The PDF collections are supplied by the caller and stay outside
this repository.

usage: sweep.py --librarian BIN [--pdfium LIB] [--jobs N] [--timeout S] FOLDER...
"""
import argparse
import collections
import json
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--librarian", required=True, type=Path)
    ap.add_argument("--pdfium", type=Path)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("folders", nargs="+", type=Path)
    args = ap.parse_args()

    files = sorted({p for folder in args.folders for p in folder.rglob("*")
                    if p.is_file() and p.suffix.lower() == ".pdf"})
    workspace = tempfile.mkdtemp(prefix="librarian-sweep-")
    base = [str(args.librarian), "--library", workspace, "parse", "--plain"]
    if args.pdfium:
        base += ["--pdfium", str(args.pdfium)]

    def run(path):
        try:
            done = subprocess.run(base + [str(path)], capture_output=True, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return path, "timeout", 0, 0.0, ""
        err = done.stderr.decode("utf-8", "replace")
        if done.returncode == 0:
            try:
                summary = json.loads(err.strip().splitlines()[-1])
                return path, "parsed", summary["pages"], summary["timing_ms"]["total"], ""
            except (ValueError, KeyError, IndexError):
                return path, "parsed without summary", 0, 0.0, err[-200:]
        if "panicked" in err:
            return path, "crash", 0, 0.0, err.strip().splitlines()[0][:200]
        if "accounting" in err:
            return path, "accounting failure", 0, 0.0, err.strip()[-200:]
        if done.returncode < 0:
            return path, f"signal {-done.returncode}", 0, 0.0, err[-200:]
        return path, "refused", 0, 0.0, err.strip()[-160:]

    started = time.time()
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(run, files))
    counts = collections.Counter(r[1] for r in results)
    pages = sum(r[2] for r in results)
    millis = sum(r[3] for r in results)
    print(f"files {len(results)}  pages {pages}  elapsed {time.time() - started:.0f} s")
    for kind, count in counts.most_common():
        print(f"  {kind:24} {count}")
    if pages:
        print(f"  parse time per page      {millis / pages:.2f} ms")
    reasons = collections.Counter(r[4].split(": ")[-1] for r in results if r[1] == "refused")
    for reason, count in reasons.most_common(8):
        print(f"  refused: {reason[:70]:70} {count}")
    bad = [r for r in results if r[1] not in ("parsed", "refused")]
    for path, kind, _, _, detail in bad[:40]:
        print(f"  {kind}: {path}  {detail}")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
