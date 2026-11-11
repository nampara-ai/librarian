#!/usr/bin/env python3
"""tags2md.py <tagged.pdf> <out.md> — turn a PDF's own structure tags into
answer-key Markdown (headings, paragraphs, list items, HTML tables).

Uses Poppler's `pdfinfo -struct-text`. Test-time only; nothing here ships.
"""
import html
import re
import subprocess
import sys


class Node:
    def __init__(self, kind, depth):
        self.kind, self.depth, self.children, self.text = kind, depth, [], []


def parse(dump):
    root = Node("Root", -1)
    stack = [root]
    for raw in dump.splitlines():
        if not raw.strip():
            continue
        depth = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.startswith("/"):
            continue  # attribute
        if line.startswith('"') and line.endswith('"'):
            # Text belongs to the nearest element above this indentation.
            while len(stack) > 1 and stack[-1].depth >= depth:
                stack.pop()
            stack[-1].children.append(line[1:-1])
            continue
        if line.startswith("Object "):
            continue
        kind = re.sub(r"\s*\((block|inline)\)", "", line).rstrip(":").strip()
        while len(stack) > 1 and stack[-1].depth >= depth:
            stack.pop()
        node = Node(kind, depth)
        stack[-1].children.append(node)
        stack.append(node)
    return root


def text_of(node):
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.kind not in ("Figure", "Formula", "Artifact"):
            parts.append(text_of(child))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


CONTAINERS = {"Root", "Document", "Art", "Sect", "Div", "Part", "TOC", "L", "NonStruct", "Private", "Index", "BibEntry", "Aside", "DocumentFragment"}


def emit(node, out):
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                out.append(re.sub(r"\s+", " ", child).strip())
            continue
        kind = child.kind
        if kind in ("Figure", "Formula", "Artifact"):
            continue
        if re.fullmatch(r"H[1-6]?", kind) or kind == "Title":
            text = text_of(child)
            if text:
                level = int(kind[1]) if len(kind) == 2 else 1
                out.append("#" * level + " " + text)
        elif kind == "LI":
            text = text_of(child)
            if text:
                out.append("- " + text)
        elif kind == "Table":
            rows = []
            def walk(n):
                for c in n.children:
                    if isinstance(c, str):
                        continue
                    if c.kind == "TR":
                        cells = [x for x in c.children if not isinstance(x, str) and x.kind in ("TH", "TD")]
                        rows.append("<tr>" + "".join(f"<{x.kind.lower()}>{html.escape(text_of(x))}</{x.kind.lower()}>" for x in cells) + "</tr>")
                    else:
                        walk(c)
            walk(child)
            if rows:
                out.append("<table>" + "".join(rows) + "</table>")
        elif kind in CONTAINERS or kind in ("TOCI",) and any(not isinstance(c, str) and c.kind in ("TOC", "TOCI") for c in child.children):
            emit(child, out)
        elif kind == "TOCI":
            text = text_of(child)
            if text:
                out.append(text)
        else:
            # P, Caption, Note, BlockQuote, Code, Lbl outside lists, ...
            has_blocks = any(not isinstance(c, str) and (c.kind in CONTAINERS or c.kind in ("P", "Table", "LI", "L") or re.fullmatch(r"H[1-6]?", c.kind)) for c in child.children)
            if has_blocks:
                emit(child, out)
            else:
                text = text_of(child)
                if text:
                    out.append(text)


def main():
    dump = subprocess.run(["pdfinfo", "-struct-text", sys.argv[1]], capture_output=True, timeout=300).stdout.decode("utf-8", "replace")
    root = parse(dump)
    out = []
    emit(root, out)
    open(sys.argv[2], "w", encoding="utf-8").write("\n\n".join(out) + "\n")
    kinds = {"h": sum(1 for o in out if o.startswith("#")), "li": sum(1 for o in out if o.startswith("- ")), "table": sum(1 for o in out if o.startswith("<table>")), "blocks": len(out)}
    print(kinds)


if __name__ == "__main__":
    main()
