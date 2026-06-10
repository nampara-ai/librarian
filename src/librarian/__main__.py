"""Module entry point so `python -m librarian` works without console scripts.

Embedded distributions (such as the bundled macOS app backend) launch the CLI
through the interpreter directly, which keeps the bundle relocatable.
"""

from librarian.cli.app import app

if __name__ == "__main__":
    app()
