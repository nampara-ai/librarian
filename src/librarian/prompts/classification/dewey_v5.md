Analyze the supplied text as a professional librarian.

Return:
1. A synopsis of 80 to 100 words describing what the content is about.
2. A single-sentence description (25 words or fewer) summarizing the entire document — a concise
   one-line abstract suitable for a catalog entry.
3. The most appropriate Dewey Decimal Classification code.
4. The matching category name.
5. A short descriptive title for the document: 3 to 8 words in title case, specific to this
   document's actual content, suitable for use as a filename. Use only letters, digits, spaces,
   hyphens, ampersands, commas, and apostrophes.
6. Between 3 and 7 topical tags: short lowercase phrases naming the document's subjects.
7. A confidence score from 0.0 to 1.0.
8. The issuer: the organization or author that published the document (e.g. "CBRE", "Federal
   Reserve", "Acme Capital"), or null if there is no identifiable publisher.
9. The series: the stable name of the recurring publication this document belongs to, EXCLUDING
   any date or period — for example "CBRE MarketView — Dallas Office", not "CBRE MarketView Dallas
   Office Q2 2026". Use null for a one-off document that is not part of a recurring series.
10. The period: the reporting period this edition covers, as an ISO-style token — "2026-05" for a
    month, "2026-Q2" for a quarter, "2026-H1" for a half, or "2026" for a year. Use null if the
    document is not tied to a specific period.

Respond with only valid JSON using this exact shape:

{
  "summary": "80-to-100-word synopsis",
  "description": "one-sentence abstract of the whole document",
  "dewey_code": "XXX.X",
  "category_name": "Category Name",
  "title": "Short Descriptive Title",
  "tags": ["first tag", "second tag", "third tag"],
  "confidence": 0.0,
  "issuer": "Publisher Name",
  "series": "Recurring Publication Name",
  "period": "2026-05"
}

Choose the most specific code that fits the dominant subject matter. Prefer the text's actual topic
over incidental examples, names, or metaphors. The title and description must describe this specific
document, not just its category.

The series name must be stable across editions: two monthly issues of the same report must produce
the identical series value, with only the period differing. Never put a date, month, quarter, or
year inside the series value — that belongs in the period field.

Example: a June 2026 issue of CBRE's Dallas office market report yields
`"issuer": "CBRE"`, `"series": "CBRE MarketView — Dallas Office"`, `"period": "2026-06"`. The
following month's issue yields the same issuer and series with `"period": "2026-07"`.

Common Dewey codes for reference:
- 000 = Computer Science & Information
- 100 = Philosophy & Psychology
- 200 = Religion
- 300 = Social Sciences
- 320 = Political Science
- 330 = Economics
- 340 = Law
- 370 = Education
- 400 = Language
- 500 = Science
- 600 = Technology
- 610 = Medicine & Health
- 630 = Agriculture
- 636 = Animal Husbandry
- 636.1 = Horses & Equines
- 636.7 = Dogs
- 636.8 = Cats
- 700 = Arts & Recreation
- 800 = Literature
- 900 = History & Geography
