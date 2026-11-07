// Expected results are deliberately authored, never regenerated from production output.
const request = action => ({ schema_version: 1, action });
const convert = (format, content) => request({ type: "convert", format, content });
const span = (source, start, end) => ({ source, start, end });
const fragment = (source, start, end, join_before = "none") => ({ span: span(source, start, end), join_before });
const block = (id, fragments, kind = { type: "paragraph" }) => ({ id, kind, fragments });
const assemble = (sources, blocks, exclusions = []) => request({ type: "assemble", document: { sources, blocks, exclusions } });

export const diagrams = [
  { source: "flowchart LR\n  A[Source] --> B[Result]", labels: ["Source", "Result"] },
  { source: 'flowchart TD\n  A["Café & 資料"] -->|yes| B["Saved"]', labels: ["Café & 資料", "Saved"] },
];
const rich = "# Report\n\n| Item | Value |\n| --- | --- |\n| A | 12 |\n\nA claim.[^note]\n\n[^note]: Source.\n\n$$\nx^2 + y^2 = z^2\n$$\n\n```mermaid\n" + diagrams[0].source + "\n```\n";
export const fixtures = [
  { name: "literal text", request: convert("text", "# Literal\n\n1. Not a list.\n\nPrice: $5."),
    markdown: "\\# Literal\n\n1\\. Not a list.\n\nPrice: \\$5.\n", stats: { headings: 0, paragraphs: 3, equations: 0 } },
  { name: "structured Markdown preserved", request: convert("markdown", rich), markdown: rich,
    stats: { headings: 1, tables: 1, footnotes: 1, equations: 1, mermaid_blocks: 1 } },
  { name: "cross-page paragraph", request: assemble(
    [{ id: "p1", text: "The sentence" }, { id: "footer", text: "7" }, { id: "p2", text: "continues." }],
    [block("paragraph", [fragment("p1", 0, 12), fragment("p2", 0, 10, "space")])],
    [{ span: span("footer", 0, 1), reason: "page_furniture" }]),
    markdown: "The sentence continues.\n", stats: { paragraphs: 1 } },
  { name: "intentional repetition", request: assemble(
    [{ id: "p1", text: "Repeat." }, { id: "p2", text: "Repeat." }],
    [block("a", [fragment("p1", 0, 7)]), block("b", [fragment("p2", 0, 7)])]),
    markdown: "Repeat.\n\nRepeat.\n" },
  { name: "partial Unicode scalar", request: assemble([{ id: "p", text: "😀" }],
    [block("a", [fragment("p", 0, 1)])]), error: "invalid_source_span" },
  { name: "missing source", request: assemble([{ id: "p", text: "abc" }],
    [block("a", [fragment("p", 0, 2)])]), error: "unaccounted_source" },
  { name: "duplicate ownership", request: assemble([{ id: "p", text: "abc" }],
    [block("a", [fragment("p", 0, 3)]), block("b", [fragment("p", 0, 3)])]), error: "duplicate_source_ownership" },
  { name: "empty input", request: convert("text", " \n\t"), error: "empty_input" },
  { name: "unsupported protocol", request: { ...convert("text", "hello"), schema_version: 2 }, error: "unsupported_schema" },
  { name: "generated Mermaid fence", request: assemble(
    [{ id: "diagram", text: diagrams[0].source }],
    [block("diagram", [fragment("diagram", 0, new TextEncoder().encode(diagrams[0].source).length)], { type: "code", language: "mermaid" })]),
    markdown: "```mermaid\n" + diagrams[0].source + "\n```\n", stats: { mermaid_blocks: 1 } },
];
