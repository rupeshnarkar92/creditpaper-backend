"""Typed errors with stable codes for the paper pipeline.

The code is the contract the front end maps to copy (spec §28); the message here
is safe to show a user. Anything diagnostic goes to the log, never to the client,
so a stack trace or a document excerpt cannot leak through the API.
"""

from __future__ import annotations


class PaperError(Exception):
    """Base. `permanent` decides retry eligibility, so it is set per subclass."""

    code = "PROCESSING_FAILED"
    message = "We couldn't process this document. Please try again."
    permanent = False

    def __init__(self, message: str | None = None, *, detail: str | None = None):
        self.detail = detail
        if message:
            self.message = message
        super().__init__(self.message)


class InvalidFile(PaperError):
    code = "INVALID_FILE"
    message = "This file could not be read."
    permanent = True


class UnsupportedFormat(PaperError):
    code = "UNSUPPORTED_FORMAT"
    message = "Only PDF or DOCX files are supported."
    permanent = True


class FileTooLarge(PaperError):
    code = "FILE_TOO_LARGE"
    message = "This file is larger than the upload limit."
    permanent = True


class FileEncrypted(PaperError):
    code = "FILE_ENCRYPTED"
    message = "This file is password-protected. Please upload an unprotected copy."
    permanent = True


class MalwareDetected(PaperError):
    code = "MALWARE_DETECTED"
    message = "This file failed our security check and was not stored."
    permanent = True


class DocumentCorrupted(PaperError):
    code = "DOCUMENT_CORRUPTED"
    message = "This document appears to be corrupted."
    permanent = True


class ExtractionFailed(PaperError):
    code = "EXTRACTION_FAILED"
    message = "We couldn't read the text in this document."
    permanent = False


class DocumentQualityLow(PaperError):
    code = "DOCUMENT_QUALITY_LOW"
    message = "This document doesn't contain enough readable text to learn from."
    permanent = True


class NotCreditPaper(PaperError):
    code = "NOT_CREDIT_PAPER"
    message = "This doesn't look like a credit paper."
    permanent = True


class ApprovalNotVerified(PaperError):
    code = "APPROVAL_NOT_VERIFIED"
    message = (
        "Approval could not be verified. The document appears to contain a recommendation, "
        "but we could not find confirmed approval evidence."
    )
    permanent = True


class FactConflict(PaperError):
    code = "FACT_CONFLICT"
    message = "Some figures in this document don't reconcile. It needs a quick human check."
    permanent = True


class LlmValidationFailed(PaperError):
    code = "LLM_VALIDATION_FAILED"
    message = "We couldn't complete the analysis of this document."
    permanent = False


class ProcessingFailed(PaperError):
    code = "PROCESSING_FAILED"
    message = "We couldn't process this document. Please try again."
    permanent = False


class PaperNotFound(PaperError):
    code = "PAPER_NOT_FOUND"
    message = "This document no longer exists."
    permanent = True


class RetryNotAllowed(PaperError):
    code = "RETRY_NOT_ALLOWED"
    message = "This document cannot be retried."
    permanent = True
