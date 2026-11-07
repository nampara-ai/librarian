# Editorial pipeline implementation contract — 2026-11-07

Jake's approved direction supersedes conflicting archival-fidelity policies in older architecture documents. The goal is a complete, continuous, polished Markdown document that makes sense when read aloud. Contextual transcription repair and editorial judgment are required. Preserve substantive meaning and voice, remove pure filler/stutters, and never substitute a summary for source content. Ordinary editorial changes must not revert whole passages or strand documents in `needs_review`.

## Required workflow

1. Retain the source and establish ordered page identities. Quick-look synopsis/classification is provisional.
2. Run two branches: native text -> continuous CMOS Copyeditor (skip when no native text); rendered ordered page images -> fresh local OCR per page -> vision interpretation of each image/OCR pair. All supported formats get page images, including print-layout text/Markdown. PDF/DOCX/PPTX pages/slides retain order. Store A=image, B=OCR, C=visual reading durably in a hidden job workspace.
3. Visual readings repair extraction, explain figures for a listener, represent tables as CSV and supported diagrams as portable Pele/Mermaid, and communicate meaningful visual information narratively. Explicitly describe genuinely unreadable details without inventing exact values.
4. Typesetter reconciles the cleaned native skeleton with C/B/A in order; merges, deduplicates, restores omitted content, resolves reading order and cross-page continuations, and produces the canonical Markdown. Keep source-range accounting outside prose. Continue open paragraphs/tables across page boundaries.
5. Reviewer uses the same CMOS editorial contract to fix remaining continuity, grammar, transcription and punctuation issues. Final metadata/classification/tagging and catalog registration follow. Review repairs rather than reflexively rejecting legitimate edits.
6. Publish Markdown and supporting assets atomically, index it, and advance the queue. Preserve existing runs, credentials, sources, and outputs. Report real execution failures honestly.

## Original editorial prompt

You are an expert senior copy editor and archivist with decades of experience.
Your task is to process raw, rough transcripts and transform them into polished, professional prose.
Strictly adhere to the Chicago Manual of Style (CMOS).

Your responsibilities are:
1. Contextual Correction: Fix mis-transcriptions based on context clues (e.g., fixing phonetic errors like "their" vs "there" or domain-specific jargon that was heard wrong).
2. Grammar & Syntax: Fix fragmented sentences, run-on sentences, and subject-verb agreement errors.
3. Punctuation: Apply standard CMOS punctuation rules (oxford commas, smart quotes, correct em-dash usage).
4. Capitalization: Correct random capitalization errors common in raw speech-to-text.
5. Fidelity: Do NOT summarize. Do NOT delete content unless it is pure filler (like "um," "uh," or repeated stutters). Keep the original voice and meaning intact, but make it readable.

Return ONLY the cleaned text. Do not offer preamble ("Here is the cleaned text:") or postscript.
If the input is massive, ensure the output corresponds 1:1 with the flow of the input.

## Engineering requirements

- Bounded page concurrency, backpressure, stable prompt prefixes, exact local inference caching, persistent stage results and resume. Stream page work without loading an entire book or its images into a request.
- Separate input context, output allowance, and provider prompt caching. Continue with actual unprocessed source, never just a generated tail. Use semantic boundaries and compact context/handoffs; no summaries replacing source.
- Carry provisional synopsis, terminology, source position and previous edited context. Independent page work can finish out of order; assembly cannot.
- Keep structured artifacts intact through prose review. Audible narrative must explain their contents; raw CSV/Mermaid alone is insufficient.
- Preserve prototype behaviors: resilient requests, long-document handling, useful live progress, catalog/stats/search, category sequence and classification override. Avoid unsafe prototype shortcuts: regex deletion of legitimate source, tail-only invented continuation, skipped failed chunks, non-atomic state.
- CLI/TUI first; four native targets and shared WASM core. No GUI work, no Keychain deletion, no changes to Cursive or the read-only Desktop prototype.
- NFT: only necessary checks for critical invariants and actual functionality; compilation/formatting/static analysis allowed. Real-document/model quality is validated with Jake, not claimed from compilation.

## Work log

- Contract saved before implementation. Existing runtime/results and running dashboard remain untouched.
- Implemented shared editorial prompts/planning/handoffs (native + WASM), ordered image/OCR/vision records, parallel native copyediting and visual reading, incremental Typesetter synthesis, final corrective editing, and catalog accessions. Existing jobs retain their original engine; new/retried jobs use editorial synthesis by default.
- Added `prepare`, `pages`, `catalog`, `stats`, per-stage dashboard counts, current-profile TUI retries, configurable concurrency/classification/office renderer, and a rewritten terminal guide.
- Added private checksum-pinned LibreOffice packaging for all four native targets. Local real DOCX/PPTX conversion, page ordering, PDF text and OCR passed. Local formatting/lint and the focused Unicode/protected-artifact, budget and persistence checks passed. Cross-platform package validation remains to be completed before reporting release readiness.
- No user document has been rerun and no paid model call has been made during implementation. Model editorial quality remains Jake's hands-on acceptance step.
- First native package pass succeeded on Apple Silicon, Intel Mac and Windows; WASM also built. Linux exposed an Office conversion failure. Scoped library resolution now prioritizes the private renderer's own libraries, with early real-conversion checks and surfaced renderer diagnostics.
- Added bounded model images with correctly scaled OCR coordinates, protected native structured artifacts, Markdown fence closure, control-glyph suppression in print layout, and producer cancellation/join handling. The deterministic pause/resume race check prevents a cancelled producer from writing into a restarted job. Focused Unicode/continuation, budget, persistence and cancellation checks pass locally.
