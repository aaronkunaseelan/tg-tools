"""Typed reader failures."""


class ExplainError(RuntimeError):
    def __init__(self, error_class: str, message: str | None = None):
        if message is None:
            message = error_class
            error_class = getattr(type(self), "code", "reader_error")
        super().__init__(message)
        self.error_class = error_class
        self.code = error_class
