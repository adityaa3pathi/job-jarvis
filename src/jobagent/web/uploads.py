"""Turn an uploaded career doc (PDF, DOCX, Markdown or text) into plain text."""

from __future__ import annotations

import io

MAX_UPLOAD_BYTES = 5_000_000


class UploadError(ValueError):
    pass


def doc_to_text(filename: str, data: bytes) -> str:
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("file is larger than 5 MB")
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        except Exception as exc:  # pypdf raises many types on broken files
            raise UploadError(f"cannot read PDF: {type(exc).__name__}") from exc
    if name.endswith(".docx"):
        from docx import Document

        try:
            doc = Document(io.BytesIO(data))
        except Exception as exc:
            raise UploadError(f"cannot read DOCX: {type(exc).__name__}") from exc
        return "\n".join(p.text for p in doc.paragraphs).strip()
    if name.endswith((".txt", ".md", ".tex")):
        try:
            return data.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise UploadError("text file is not UTF-8") from exc
    raise UploadError("use a PDF, DOCX, TXT or MD file")
