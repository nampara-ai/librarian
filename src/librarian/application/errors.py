"""Application errors shared without circular service dependencies."""


class ProcessingCanceled(RuntimeError):
    """Raised when an operator cancels a processing run."""
