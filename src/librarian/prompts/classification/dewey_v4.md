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

Respond with only valid JSON using this exact shape:

{
  "summary": "80-to-100-word synopsis",
  "description": "one-sentence abstract of the whole document",
  "dewey_code": "XXX.X",
  "category_name": "Category Name",
  "title": "Short Descriptive Title",
  "tags": ["first tag", "second tag", "third tag"],
  "confidence": 0.0
}

Choose the most specific code that fits the dominant subject matter. Prefer the text's actual topic
over incidental examples, names, or metaphors. The title and description must describe this specific
document, not just its category.

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
