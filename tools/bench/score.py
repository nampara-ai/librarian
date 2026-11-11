#!/usr/bin/env python3
"""Score Markdown predictions against ground-truth Markdown.

Independent implementation of the metric definitions published by
opendataloader-bench (NID reading order, TEDS tables, MHS headings), written
for Librarian's own regression runs. It reads only Markdown data files.

usage: score.py <ground-truth-dir> <prediction-markdown-dir> [--json out.json] [--worst N]
"""
import json
import re
import sys
from collections import deque
from html import escape, unescape
from multiprocessing import Pool
from pathlib import Path
from statistics import fmean

from apted import APTED, Config
from bs4 import BeautifulSoup
from lxml import etree, html
from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein


# ---- Markdown tables -> HTML -------------------------------------------------
def split_row(line):
    t = line.strip()
    if not t or "|" not in t:
        return None
    cells = t.split("|")
    if t.startswith("|"):
        cells = cells[1:]
    if cells and t.endswith("|"):
        cells = cells[:-1]
    cells = [c.strip() for c in cells]
    return cells or None


def is_separator(cells):
    if not cells:
        return False
    for c in cells:
        c = c.replace(" ", "")
        if not c or not set(c) <= {"-", ":"}:
            return False
    return True


def fit(cells, width):
    if width <= 0 or len(cells) == width:
        return cells
    if len(cells) == 3 and width > 3:
        return [cells[0]] + [cells[1]] * (width - 2) + [cells[2]]
    if len(cells) < width:
        return cells + [""] * (width - len(cells))
    return cells[:width]


def tables_to_html(md):
    if not md:
        return md
    lines = md.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        head = split_row(lines[i])
        if not head or i + 1 >= len(lines):
            out.append(lines[i]); i += 1; continue
        sep = split_row(lines[i + 1])
        if not sep or not is_separator(sep):
            out.append(lines[i]); i += 1; continue
        width = max(len(head), len(sep))
        head = fit(head, width)
        rows, j = [], i + 2
        while j < len(lines):
            r = split_row(lines[j])
            if not r:
                break
            rows.append(fit(r, width)); j += 1
        if head and all(c == "" for c in head) and rows:
            head = rows.pop(0)
        table = ("<table><tr>" + "".join(f"<th>{escape(c)}</th>" for c in head) + "</tr>"
                 + "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r) + "</tr>" for r in rows)
                 + "</table>")
        seg = lines[i]
        ending = "\r\n" if seg.endswith("\r\n") else "\n" if seg.endswith("\n") else "\r" if seg.endswith("\r") else ""
        out.append(table + ending)
        i = j
    return "".join(out)


def squash(text):
    return re.sub(r"\s+", " ", text).strip()


# ---- Reading order -----------------------------------------------------------
TABLE_RE = re.compile(r"<table[^>]*?>.*?</table>", re.I | re.S)


def reading_order(gt, pred):
    g = tables_to_html(gt)
    if not squash(g):
        return None, None
    p = tables_to_html(pred)
    return (fuzz.ratio(squash(g), squash(p)) / 100.0,
            fuzz.ratio(squash(TABLE_RE.sub(" ", g or "")), squash(TABLE_RE.sub(" ", p or ""))) / 100.0)


# ---- Headings ----------------------------------------------------------------
class Node:
    def __init__(self, tag, text=None):
        self.tag, self.text, self.children = tag, text, []


class HeadingCost(Config):
    def __init__(self, with_text):
        self.with_text = with_text

    def rename(self, a, b):
        if a.tag != b.tag:
            return 1.0
        if not self.with_text:
            return 0.0
        x, y = a.text or "", b.text or ""
        if not x and not y:
            return 0.0
        return Levenshtein.distance(x, y) / float(max(len(x), len(y), 1))


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def heading_tree(md):
    root = Node("document")
    if not md:
        return root
    box, pending = root, []

    def flush():
        text = squash(" ".join(pending))
        if text:
            box.children.append(Node("content", text))
        pending.clear()

    for raw in md.splitlines():
        m = HEADING_RE.match(raw)
        if m:
            flush()
            node = Node("heading", squash(m.group(2)))
            root.children.append(node)
            box = node
            continue
        n = squash(raw)
        if n:
            pending.append(n)
    flush()
    return root


def count(node):
    return 1 + sum(count(c) for c in node.children)


def headings(gt, pred):
    g = heading_tree(tables_to_html(gt))
    if not any(c.tag == "heading" for c in g.children):
        return None, None
    p = heading_tree(tables_to_html(pred))
    if not any(c.tag == "heading" for c in p.children):
        return 0.0, 0.0
    n = max(count(g), count(p), 1)
    a = float(APTED(g, p, HeadingCost(True)).compute_edit_distance())
    b = float(APTED(g, p, HeadingCost(False)).compute_edit_distance())
    clamp = lambda v: max(0.0, min(1.0, v))
    return clamp(1 - a / n), clamp(1 - b / n)


# ---- Tables ------------------------------------------------------------------
class Cell:
    def __init__(self, tag, colspan=None, rowspan=None, content=None):
        self.tag, self.colspan, self.rowspan, self.content, self.children = tag, colspan, rowspan, content, []


def cell_text(text):
    return squash(re.sub(r"<br\s*/?>", "\n", unescape(text)))


class TableCost(Config):
    def rename(self, a, b):
        if a.tag != b.tag or a.colspan != b.colspan or a.rowspan != b.rowspan:
            return 1.0
        if a.tag == "td" and (a.content or b.content):
            x, y = cell_text("".join(a.content or [])), cell_text("".join(b.content or []))
            if not x and not y:
                return 0.0
            return Levenshtein.distance(x, y) / float(max(len(x), len(y)))
        return 0.0


def tokens(node, acc):
    acc.append("<%s>" % node.tag)
    if node.text is not None:
        acc.extend(node.text)
    for child in node.getchildren():
        tokens(child, acc)
    if node.tag != "unk":
        acc.append("</%s>" % node.tag)
    if node.tag != "td" and node.tail is not None:
        acc.extend(node.tail)


def table_tree(node, structure_only, parent=None):
    if node.tag == "td":
        content = []
        if not structure_only:
            acc = []
            tokens(node, acc)
            content = acc[1:-1]
        new = Cell("td", int(node.attrib.get("colspan", "1")), int(node.attrib.get("rowspan", "1")), content)
    else:
        new = Cell(node.tag)
    if parent is not None:
        parent.children.append(new)
    if node.tag != "td":
        for child in node.getchildren():
            table_tree(child, structure_only, new)
    return new


def wrap(s):
    if s.startswith("<table>") and s.endswith("</table>"):
        s = "<html><body>" + s + "</body></html>"
    elif not s.startswith("<html><body><table>") and not s.endswith("</table></body></html>"):
        s = "<html><body><table>" + s + "</table></body></html>"
    for tok in ("<thead>", "</thead>", "<tbody>", "</tbody>"):
        s = s.replace(tok, "")
    return s


def teds(gt_html, pred_html, structure_only):
    pred, true = wrap(pred_html), wrap(gt_html)
    if not pred or not true:
        return 0.0
    parser = html.HTMLParser(remove_comments=True, encoding="utf-8")
    pred = html.fromstring(pred, parser=parser)
    true = html.fromstring(true, parser=parser)
    if not (pred.xpath("body/table") and true.xpath("body/table")):
        return 0.0
    pred, true = pred.xpath("body/table")[0], true.xpath("body/table")[0]
    for t in (pred, true):
        for th in t.xpath(".//th"):
            th.tag = "td"
    n = max(len(pred.xpath(".//*")), len(true.xpath(".//*")))
    d = APTED(table_tree(pred, structure_only), table_tree(true, structure_only), TableCost()).compute_edit_distance()
    return 1.0 - float(d) / n


def extract(md):
    return [str(t) for t in BeautifulSoup(md, "html.parser").find_all("table")]


def tables(gt, pred):
    g, p = extract(tables_to_html(gt)), extract(tables_to_html(pred))
    if not g:
        return None, None
    if not p:
        return 0.0, 0.0
    gd = "<html><body>\n" + "\n".join(g) + "\n</body></html>"
    pd = "<html><body>\n" + "\n".join(p) + "\n</body></html>"
    return teds(gd, pd, False), teds(gd, pd, True)


# ---- Driver ------------------------------------------------------------------
def read(path):
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return ""


def one(args):
    gt_path, pred_path = args
    gt, pred = read(gt_path), read(pred_path)
    nid, nid_s = reading_order(gt, pred)
    t, t_s = tables(gt, pred)
    m, m_s = headings(gt, pred)
    parts = [v for v in (nid, t, m) if v is not None]
    return {"id": gt_path.stem, "overall": fmean(parts) if parts else None,
            "nid": nid, "nid_s": nid_s, "teds": t, "teds_s": t_s, "mhs": m, "mhs_s": m_s}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    gt_dir, pred_dir = Path(args[0]), Path(args[1])
    jobs = [(g, pred_dir / g.name) for g in sorted(gt_dir.glob("*.md"))]
    with Pool(4) as pool:
        docs = pool.map(one, jobs, chunksize=4)
    keys = ["overall", "nid", "nid_s", "teds", "teds_s", "mhs", "mhs_s"]
    mean = {}
    for k in keys:
        vals = [d[k] for d in docs if d[k] is not None]
        mean[k] = fmean(vals) if vals else None
    print("  ".join(f"{k}={mean[k]:.4f}" if mean[k] is not None else f"{k}=n/a" for k in keys))
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps({"mean": mean, "documents": docs}, indent=1))
    if "--worst" in sys.argv:
        n = int(sys.argv[sys.argv.index("--worst") + 1])
        for k in ("nid", "teds", "mhs"):
            ranked = sorted((d for d in docs if d[k] is not None), key=lambda d: d[k])[:n]
            print(k, "worst:", " ".join(f"{d['id'][-4:]}:{d[k]:.2f}" for d in ranked))


if __name__ == "__main__":
    main()
