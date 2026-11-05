"""PDF utility interfaces."""

from pathlib import Path


def get_page_count(source: Path) -> int:
    """Return a PDF page count using the configured PDF backend.

    TODO: implement with PyMuPDF and translate parser errors to domain exceptions.
    """
    raise NotImplementedError("TODO: implement PDF page counting.")
