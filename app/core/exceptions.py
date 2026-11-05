"""Domain exceptions for clear API and pipeline error handling."""


class RAGError(Exception):
    """Base exception for expected application failures."""


class ConfigurationError(RAGError):
    """Raised when required runtime configuration is invalid or missing."""


class DocumentProcessingError(RAGError):
    """Raised when a source document cannot be parsed safely."""


class PDFValidationError(DocumentProcessingError):
    """Raised when a PDF is missing, unreadable, corrupt, or empty."""


class PasswordProtectedPDFError(PDFValidationError):
    """Raised when a PDF requires a password that was not provided."""


class PDFExtractionError(DocumentProcessingError):
    """Raised when PDF page extraction fails before a page record is created."""


class RetrievalError(RAGError):
    """Raised when document search fails."""


class GenerationError(RAGError):
    """Raised when an LLM provider cannot generate an answer."""
