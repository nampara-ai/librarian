#!/usr/bin/env python3
"""Compare Librarian's parse of tagged PDFs with the structure their authors
declared. Each PDF's own structure tags are read with Poppler's
`pdfinfo -struct-text` and turned into answer-key Markdown; headings and list
items are then matched by normalised text, tables by count and cell text.

The tagged PDFs are supplied by the caller and stay outside this repository.

usage: tagged.py --librarian BIN --pdfs DIR --out DIR [--pdfium LIB]
"""
import argparse
import collections
import re
import shutil
import subprocess
import sys
from pathlib import Path

from rapidfuzz import fuzz

HERE = Path(__file__).resolve().parent

norm=lambda s: re.sub(r"[^0-9a-z\u00c0-\ud7ff]+","",s.lower())
def blocks(md):
    md=re.sub(r"<table>.*?</table>", lambda m: "\n\n"+m.group(0).replace("\n"," ")+"\n\n", md, flags=re.S)
    out=[]
    for b in re.split(r"\n\s*\n", md):
        b=b.strip()
        if b: out.append(b)
    return out
def kinds(md):
    h,li,tables,cells=[],[],0,collections.Counter()
    for b in blocks(md):
        if b.startswith("#"): h.append(norm(b.lstrip("#")))
        elif b.startswith("<table"):
            tables+=1
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", b, re.S):
                k=norm(re.sub(r"<[^>]+>"," ",c))
                if k: cells[k]+=1
        elif b.startswith("|"):
            tables+=1
            for row in b.splitlines():
                if set(row.replace("|","").strip())<=set("-: "): continue
                for c in row.strip().strip("|").split("|"):
                    k=norm(c)
                    if k: cells[k]+=1
        else:
            for line in b.splitlines():
                if line.startswith("- "): li.append(norm(re.sub(r"^-\s*[•◦▪·*–-]?\s*","",line))[:60])
    return h,li,tables,cells
def prf(truth,ours):
    t,o=collections.Counter(truth),collections.Counter(ours)
    hit=sum((t&o).values())
    p=hit/max(sum(o.values()),1); r=hit/max(sum(t.values()),1)
    return p,r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--librarian", required=True, type=Path)
    ap.add_argument("--pdfs", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--pdfium", type=Path)
    args = ap.parse_args()
    if args.out.exists():
        shutil.rmtree(args.out)
    (args.out / "truth").mkdir(parents=True)
    for pdf in sorted(args.pdfs.glob("*.pdf")):
        subprocess.run([sys.executable, "-I", str(HERE / "tags2md.py"), str(pdf),
                        str(args.out / "truth" / (pdf.stem + ".md"))], check=True)
    command = [str(args.librarian), "--library", str(args.out / "workspace"), "parse",
               str(args.pdfs), "-o", str(args.out / "md"), "--create-output", "--plain"]
    if args.pdfium:
        command += ["--pdfium", str(args.pdfium)]
    run = subprocess.run(command, capture_output=True, text=True)
    print(run.stdout.strip().splitlines()[0] if run.stdout.strip() else run.stderr.strip()[-300:])
    print(f"{'document':34} {'headings P/R (truth,ours)':28} {'list items P/R':22} "
          f"{'tables truth/ours':18} {'cell text P/R':14} text")
    for truth in sorted((args.out / "truth").glob("*.md")):
        ours = args.out / "md" / truth.name
        if not ours.exists():
            continue
        t, o = truth.read_text(encoding="utf-8"), ours.read_text(encoding="utf-8")
        th, tl, tt, tc = kinds(t)
        oh, ol, ot, oc = kinds(o)
        hp, hr = prf(th, oh)
        lp, lr = prf(tl, ol)
        shared = sum((tc & oc).values())
        cp, cr = shared / max(sum(oc.values()), 1), shared / max(sum(tc.values()), 1)
        plain = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>|[#|]", " ", s))
        a, c = plain(t), plain(o)
        sim = fuzz.ratio(a[:300000], c[:300000]) / 100 if a else float("nan")
        cell = lambda p, r, n, m: f"{p:.2f}/{r:.2f} ({n},{m})" if n or m else "  -"
        print(f"{truth.stem[:34]:34} {cell(hp, hr, len(th), len(oh)):28} "
              f"{cell(lp, lr, len(tl), len(ol)):22} {str(tt) + '/' + str(ot):18} "
              f"{(f'{cp:.2f}/{cr:.2f}' if tt else '  -'):14} {sim:.3f}")


if __name__ == "__main__":
    main()
