"""Text from the PDF text layer (step 2). No OCR in v0."""

from __future__ import annotations

import io

from pypdf import PdfReader


def extract_text(data: bytes, content_type: str) -> tuple[str, int]:
    """Return (text, page_count). Plain text passes through as one page."""
    if content_type == "application/pdf" or data[:5] == b"%PDF-":
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages), len(pages)
    return data.decode("utf-8", errors="replace"), 1
