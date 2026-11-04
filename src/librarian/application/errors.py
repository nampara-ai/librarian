"""Application errors shared without circular service dependencies."""


class ProcessingCanceled(RuntimeError):
    """Raised when an operator cancels a processing run."""


class LLMResponseTruncatedError(RuntimeError):
    """A provider exhausted its output budget; partial text is not an accepted answer."""

    def __init__(self, message: str, *, partial_text: str = "") -> None:
        super().__init__(message)
        self.partial_text = partial_text
