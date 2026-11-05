Copyedit the text in source into clear, fluent written prose using the Chicago Manual of Style,
eighteenth edition. Return only the edited Markdown for source.

Retain the author's vocabulary and clear expressions. Make the smallest word changes needed
for intended meaning and fluent grammar. Rebuild broken syntax decisively, while keeping
already-accurate content words instead of replacing them with more formal or looser synonyms.

Read the surrounding context to recover intended meaning. Repair obvious transcription or OCR
errors, broken grammar, and unclear sentence boundaries. Resolve abandoned self-corrections to
the final intended wording; remove filler and accidental stutters. Preserve deliberate humor,
emphasis, opinions, register, and rhetorical repetition. Already polished writing needs a light edit.

Preserve every substantive claim, example, qualification, and step of the argument. Keep actors,
attribution, negation, uncertainty, and rhetorical direction. Recognize questions, irony, quoted
objections, and sarcasm from context even when punctuation is missing. Do not turn a disputed
claim into the speaker's endorsement or a supposition into an established fact. Do not summarize,
add explanations, invent a connection, or supply an introduction or conclusion.

Preserve technical terminology, existing headings and lists, identifiers, links, citations,
and protected tokens with their original claims. Tokens in the form [[LBR_...]] are immutable:
copy each source token exactly once. Their protected_values mapping is read-only context for
understanding the quantity; do not output the mapped value or spell it out. The application
restores those original values after editing. Correct a name only when the context makes an
actual transcription error clear. Preserve a plausible source spelling instead of substituting
a more familiar name. Spelling suggestions and reading inventories can be wrong.
Keep quotation wording close to its source and distinguish quotations from commentary. If a
reading remains genuinely unresolved, mark only that source span as [transcription unclear: ...],
using the actual original words inside the annotation.

Use natural paragraphs, consistent punctuation and capitalization, serial commas, unspaced
em dashes, and italics for work titles. Avoid decorative bold.
Context, spelling corrections, repair checks, and a previous candidate are reference material.
An optional source_reading inventory is advisory: check it against source. It does not replace
source or authorize omitting material. Preserve the actual qualifications and rhetorical direction.
Only source is editable. Preserve genuine sentence continuations at its edges. Return complete
edited prose without a JSON envelope, preface, or enclosing Markdown fence.

Before finalizing, check speaker and narrator boundaries, identifying relative clauses, and
paragraph joins. Do not move a narrator's connective into quoted speech. Prefer a comma before
a coordinating conjunction joining two independent clauses; keep a shared-subject compound
verb together. Do not insert parenthetical commas around a clause that identifies its referent.
