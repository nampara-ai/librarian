# Open Knowledge Format (OKF) export

Librarian can render a processed corpus as an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
v0.1 bundle: a vendor-neutral directory of markdown "concept" files with YAML
frontmatter that any OKF-aware agent or tool can consume without a translation
layer. This turns a pile of cleaned documents into a portable, navigable
knowledge bundle.

OKF is a format, not a dependency — there is no SDK to install. Librarian emits
conformant bundles directly; it pins the version it targets via
`okf_version: "0.1"` in the bundle-root `index.md`.

## Producing a bundle

CLI (for agents and bulk runs):

```bash
librarian export-okf ./bundle              # all processed documents
librarian export-okf ./bundle --classification-prefix 6   # only Dewey 6xx
librarian export-okf ./bundle --tag horses --limit 50 --json
```

`export-okf` includes only documents that have been processed (have a cleaned
output and a classification); unprocessed documents are reported as skipped. The
command exits non-zero when no documents match.

HTTP:

- `GET /export/okf?classification_prefix=&tag=&limit=` → `{ "okf_version", "files": {path: content}, "skipped": [...] }`
- `GET /documents/{id}/okf` → `{ "path", "content" }` for a single concept

## Bundle layout

Documents are placed into a directory tree derived from their Dewey
classification, so the directory structure doubles as a browsable taxonomy:

```
index.md                                  # bundle root; declares okf_version
600-technology/
  index.md
  630-agriculture/
    636-animal-husbandry/
      636-1-horses-equines/
        index.md
        saddle-fit-and-groundwork-notes.md
```

- The **concept ID** is the file path minus `.md` (per the spec).
- Each directory gets an `index.md` listing its concepts and subsections for
  progressive disclosure. Only the bundle-root `index.md` carries frontmatter
  (the `okf_version` declaration), as the spec requires.
- Concepts that share a Dewey code are cross-linked under a `## Related`
  section, forming a graph richer than the directory tree.

## Field mapping

Each concept's frontmatter is rendered from Librarian's classification and
document metadata:

| OKF field | Source | Notes |
| --- | --- | --- |
| `type` (required) | source document kind | e.g. `PDF Document`, `Transcript`, `Word Document`, `Scanned Image`, else `Document` |
| `title` | classification title | falls back to the source filename stem |
| `description` | classification one-sentence abstract | falls back to the first sentence of the synopsis |
| `resource` | `urn:librarian:doc:{id}` | stable, portable identity (not a local path) |
| `tags` | classification tags | |
| `timestamp` | cleaned-output creation time | ISO-8601 UTC |
| `dewey_code` / `dewey_label` | classification | extension fields |
| `source_filename` | original filename | extension field |
| `classification_confidence` | classification confidence | extension field |

The body is the cleaned markdown, preceded by the full synopsis as a blockquote.

## Conformance

A bundle is OKF v0.1 conformant if every non-reserved `.md` file has a parseable
YAML frontmatter block with a non-empty `type`. Librarian guarantees this for
every concept it emits; the test suite parses every emitted file and asserts it.
Everything else in the spec is advisory, and consumers must tolerate missing
optional fields, unknown types, and broken links.

## Versioning

OKF is versioned with a backward-compatible growth promise. Librarian targets
**v0.1** today and declares it in the bundle root. If the upstream spec
revises, the producer is updated in a contained change and the declared version
is bumped accordingly.
