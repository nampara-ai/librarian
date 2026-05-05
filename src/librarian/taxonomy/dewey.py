"""Dewey-inspired taxonomy defaults."""

from __future__ import annotations


class DeweyTaxonomy:
    """Small pragmatic Dewey map for the first release."""

    name = "dewey"

    _labels = {
        "000": "Computer Science & Information",
        "100": "Philosophy & Psychology",
        "200": "Religion",
        "300": "Social Sciences",
        "400": "Language",
        "500": "Science",
        "600": "Technology",
        "610": "Medicine & Health",
        "630": "Agriculture",
        "636": "Animal Husbandry",
        "636.1": "Horses & Equines",
        "636.7": "Dogs",
        "636.8": "Cats",
        "700": "Arts & Recreation",
        "800": "Literature",
        "900": "History & Geography",
    }

    def label_for(self, code: str) -> str | None:
        return self._labels.get(code)

    def all(self) -> dict[str, str]:
        return dict(self._labels)
