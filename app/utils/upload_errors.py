"""
Centralised upload error vocabulary for the InCite upload pipeline.

Why a separate module?
- Keeps error codes in one place — no scattered string literals to keep in sync.
- Custom exception carries both a machine code (for the DB / API client) and a
  human message (for the frontend) without needing callers to assemble them.
- The route handler and the ingestion service both import from here, so the
  codes are guaranteed to be consistent between synchronous validation and any
  secondary checks that run in the background.
"""

import enum


class UploadErrorCode(str, enum.Enum):
    """Machine-readable codes stored on the Document row and returned to the
    frontend.  The string values are stable API surface — do not rename them."""

    INVALID_FILE = "INVALID_FILE"
    """Uploaded bytes are not a recognisable PDF (fitz could not open them)."""

    INVALID_PDF = "INVALID_PDF"
    """File has a PDF header but the internal structure is corrupted."""

    PASSWORD_PROTECTED = "PASSWORD_PROTECTED"
    """PDF is encrypted with a user password."""

    EMPTY_DOCUMENT = "EMPTY_DOCUMENT"
    """PDF opened successfully but contains zero pages."""

    NO_EXTRACTABLE_TEXT = "NO_EXTRACTABLE_TEXT"
    """PDF is valid but every page is an image; the current pipeline has no OCR."""

    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    """Raw file size exceeds MAX_UPLOAD_SIZE_MB."""

    PAGE_LIMIT_EXCEEDED = "PAGE_LIMIT_EXCEEDED"
    """PDF page count exceeds MAX_PDF_PAGES."""

    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    """A recoverable resource error (e.g. MemoryError) occurred during ingestion."""

    PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"
    """Ingestion exceeded the configured time limit."""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    """An unexpected error occurred.  Details are logged server-side only."""


# User-facing messages keyed by error code.
# Shown in the frontend — must never contain stack traces or internal paths.
_USER_MESSAGES: dict[UploadErrorCode, str] = {
    UploadErrorCode.INVALID_FILE: (
        "This file could not be read as a PDF. Please upload a valid PDF document."
    ),
    UploadErrorCode.INVALID_PDF: (
        "This PDF appears to be corrupted. Please try re-exporting or re-downloading it."
    ),
    UploadErrorCode.PASSWORD_PROTECTED: (
        "This PDF is password-protected. Remove the password from the PDF and try uploading it again."
    ),
    UploadErrorCode.EMPTY_DOCUMENT: (
        "This PDF contains no pages. Please upload a document with content."
    ),
    UploadErrorCode.NO_EXTRACTABLE_TEXT: (
        "This PDF contains only scanned images. OCR is not currently supported."
    ),
    UploadErrorCode.FILE_TOO_LARGE: (
        "This file exceeds the maximum upload size. Please upload a smaller PDF."
    ),
    UploadErrorCode.PAGE_LIMIT_EXCEEDED: (
        "This PDF exceeds the maximum page limit. Please upload a shorter document."
    ),
    UploadErrorCode.RESOURCE_LIMIT: (
        "Processing could not complete due to resource constraints. "
        "Please try again or upload a smaller document."
    ),
    UploadErrorCode.PROCESSING_TIMEOUT: (
        "Processing timed out. Please try again or upload a smaller document."
    ),
    UploadErrorCode.INTERNAL_ERROR: (
        "An unexpected error occurred while processing your document. "
        "Please try again. If the problem persists, contact support."
    ),
}


class UploadValidationError(Exception):
    """
    Raised by the PDF validator and the ingestion service when a document
    cannot be processed.

    Attributes:
        error_code: Machine-readable UploadErrorCode.
        user_message: Safe, human-readable string suitable for display in the UI.
    """

    def __init__(
        self,
        error_code: UploadErrorCode,
        user_message: str | None = None,
    ) -> None:
        self.error_code = error_code
        # Allow callers to supply a custom message (e.g. to include the
        # configured limit in the error text) while falling back to the
        # centralised default.
        self.user_message = user_message or _USER_MESSAGES[error_code]
        super().__init__(self.user_message)
