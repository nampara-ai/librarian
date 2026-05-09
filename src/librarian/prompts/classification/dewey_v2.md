Analyze the supplied text as a professional librarian.

Return:
1. A concise summary of 80 words or fewer describing what the content is about.
2. The most appropriate Dewey Decimal Classification code.
3. The matching category name.
4. A confidence score from 0.0 to 1.0.

Respond with only valid JSON using this exact shape:

{
  "summary": "80-word-or-fewer summary",
  "dewey_code": "XXX.X",
  "category_name": "Category Name",
  "confidence": 0.0
}

Choose the most specific code that fits the dominant subject matter. Prefer the text's actual topic
over incidental examples, names, or metaphors.

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
