from pathlib import Path
from .heuristics import extract_structured as _heuristic_extract

# Backward-compatible re-export (tests import from app.extract).
extract_structured = _heuristic_extract


def text_from_file(path: Path):
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    if path.suffix.lower() == ".docx":
        from docx import Document
        return "\n".join(p.text for p in Document(str(path)).paragraphs)
    raise ValueError("Format accepté: PDF, DOCX ou TXT")
