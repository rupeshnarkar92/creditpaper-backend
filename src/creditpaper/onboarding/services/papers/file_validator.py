"""Deterministic upload validation. No LLM, no network, no guessing.

Runs before anything is stored, and again as the pipeline's first stage so a
retry re-checks the bytes on disk rather than trusting the earlier verdict.

The browser's `Content-Type` is advisory only: it is attacker-controlled. The
decision comes from the magic bytes, and the extension must agree with them —
`malware.exe` renamed to `paper.pdf` fails on signature, and a real PDF renamed
`.docx` fails on the mismatch.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from creditpaper.onboarding.services.papers import config
from creditpaper.onboarding.services.papers.errors import (
    DocumentCorrupted,
    FileEncrypted,
    FileTooLarge,
    InvalidFile,
    PaperError,
    UnsupportedFormat,
)

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"

ALLOWED_EXTS = {".pdf", ".docx"}

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# A DOCX is a zip that must contain these. A generic zip renamed `.docx` does not.
DOCX_REQUIRED_MEMBERS = ("[Content_Types].xml", "word/document.xml")


@dataclass
class FileValidation:
    ok: bool
    mime_type: str = ""
    extension: str = ""
    size: int = 0
    page_count: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    notes: list[str] = field(default_factory=list)


class DocumentFileValidator:
    """`validate(filename, content)` -> FileValidation. Never raises for a bad
    document; raising is reserved for programmer error."""

    def validate(self, filename: str, content: bytes) -> FileValidation:
        try:
            return self._validate(filename, content)
        except PaperError as e:
            return FileValidation(ok=False, error_code=e.code, error_message=e.message)

    # -- internals ---------------------------------------------------------

    def _validate(self, filename: str, content: bytes) -> FileValidation:
        if not filename or not content:
            raise InvalidFile()

        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTS:
            raise UnsupportedFormat()

        size = len(content)
        if size > config.max_upload_size():
            raise FileTooLarge()
        if size < config.min_file_size():
            raise InvalidFile("This file is empty or too small to be a credit paper.")

        detected = self._detect(content)
        if detected is None:
            raise UnsupportedFormat()
        if detected != ext:
            raise UnsupportedFormat(
                "This file's contents don't match its extension. Please re-export and try again."
            )

        if ext == ".pdf":
            page_count = self._check_pdf(content)
        else:
            page_count = self._check_docx(content)

        return FileValidation(
            ok=True,
            mime_type=MIME_BY_EXT[ext],
            extension=ext,
            size=size,
            page_count=page_count,
        )

    def _detect(self, content: bytes) -> str | None:
        """Extension implied by the signature, not by the name."""
        head = content[:8]
        if head.startswith(PDF_MAGIC):
            return ".pdf"
        if head.startswith(ZIP_MAGIC):
            return ".docx"
        return None

    def _check_pdf(self, content: bytes) -> int | None:
        """Integrity, encryption and page count without a PDF library.

        Deliberately signature-level: the project reads PDFs natively through the
        model (DECISIONS.md 2026-06-16, no separate parse/OCR vendor), so adding a
        parser here would introduce a dependency the pipeline does not otherwise
        need. What is checked is what a structural gate can honestly check.
        """
        if b"%%EOF" not in content[-4096:] and b"%%EOF" not in content:
            raise DocumentCorrupted()

        # /Encrypt in the trailer means the content stream is encrypted. A
        # user-password PDF cannot be read at all; an owner-password one often
        # can, but we refuse both rather than guess which restriction applies.
        if b"/Encrypt" in content:
            raise FileEncrypted()

        pages = content.count(b"/Type /Page") + content.count(b"/Type/Page")
        page_count = pages if pages > 0 else None
        if page_count and page_count > config.max_pages():
            raise DocumentCorrupted(
                f"This document has more than {config.max_pages()} pages, which is beyond our limit."
            )
        return page_count

    def _check_docx(self, content: bytes) -> int | None:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = set(zf.namelist())
                if bad := zf.testzip():
                    raise DocumentCorrupted(f"This document is corrupted (bad entry: {bad}).")
                for required in DOCX_REQUIRED_MEMBERS:
                    if required not in names:
                        raise UnsupportedFormat()
                # An OOXML file protected with a password is an OLE container, not
                # a readable zip — but a write-protected one is a normal zip with
                # documentProtection set, and that is still readable, so it passes.
                if "EncryptedPackage" in names or "EncryptionInfo" in names:
                    raise FileEncrypted()
        except zipfile.BadZipFile as e:
            raise DocumentCorrupted() from e
        return None


validator = DocumentFileValidator()


def validate_upload(filename: str, content: bytes) -> FileValidation:
    """Module-level entry point, so callers need no instance."""
    return validator.validate(filename, content)
