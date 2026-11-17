class AppError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message = message
        self.status = status


class UpstreamError(AppError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message, status)
