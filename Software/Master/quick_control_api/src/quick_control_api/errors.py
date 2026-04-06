from __future__ import annotations


class QuickControlError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = str(code or "INTERNAL_ERROR")
        self.message = str(message or code or "error")
        self.status_code = int(status_code)
