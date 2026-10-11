from .models import ErrorCode


class DomainError(Exception):
    """Only fixed, non-sensitive messages cross the MCP boundary."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
