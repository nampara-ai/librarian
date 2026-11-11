#!/usr/bin/env python3
"""Parse the opendataloader-bench documents with Librarian and score the result.

The benchmark checkout is supplied by the caller and stays outside this
repository. Odd-numbered documents are the development half. Even-numbered
documents are the holdout half: read its aggregate only.

usage: benchmark.py --librarian BIN --bench DIR --out DIR [--pdfium LIB]

  --librarian  path of the librarian executable
  --bench      checkout of opendataloader-bench (pdfs/, ground-truth/markdown/,
               prediction/<engine>/evaluation.json)
  --out        empty or new folder for the Markdown, JSON and scores
  --pdfium     PDFium library to use instead of the installed engine
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
METRICS = ("overall", "nid", "teds", "mhs")


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--librarian", required=True, type=Path)
    ap.add_argument("--bench", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--pdfium", type=Path)
    args = ap.parse_args()

    if args.out.exists():
        shutil.rmtree(args.out)
    (args.out / "json").mkdir(parents=True)
    command = [str(args.librarian), "--library", str(args.out / "workspace"), "parse",
               str(args.bench / "pdfs"), "-o", str(args.out / "md"), "--create-output",
               "--plain", "--furniture", "--words"]
    if args.pdfium:
        command += ["--pdfium", str(args.pdfium)]
    started = time.time()
    run = subprocess.run(command, capture_output=True, text=True)
    print(f"parse: exit {run.returncode}, {time.time() - started:.1f} s")
    print(run.stdout.strip().splitlines()[0] if run.stdout.strip() else run.stderr.strip()[-300:])
    for path in (args.out / "md").glob("*.layout.json"):
        path.rename(args.out / "json" / path.name)

    scores = args.out / "scores.json"
    subprocess.run([sys.executable, "-I", str(HERE / "score.py"),
                    str(args.bench / "ground-truth" / "markdown"), str(args.out / "md"),
                    "--json", str(scores)], check=True, stdout=subprocess.DEVNULL)
    ours = {d["id"]: d for d in json.loads(scores.read_text())["documents"]}

    engines = {}
    for path in sorted((args.bench / "prediction").glob("*/evaluation.json")):
        docs = json.loads(path.read_text())["documents"]
        engines[path.parent.name] = {d["document_id"]: d["scores"] for d in docs}

    for split in ("development", "holdout", "all"):
        ids = [i for i in sorted(ours)
               if split == "all" or (int(i) % 2 == 1) == (split == "development")]
        print(f"\n{split}: {len(ids)} documents")
        print(f"{'engine':34}" + "".join(f"{m:>9}" for m in METRICS))
        rows = [("librarian", [mean(ours[i][m] for i in ids) for m in METRICS])]
        for name, docs in engines.items():
            rows.append((name, [mean(docs[i][m] for i in ids if i in docs) for m in METRICS]))
        for name, values in sorted(rows, key=lambda r: -r[1][0]):
            print(f"{name:34}" + "".join(f"{v:9.4f}" for v in values))


if __name__ == "__main__":
    main()
