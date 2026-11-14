"""Errors of the API: an HTTP status, a stable code the interface translates, and an English message (D10)."""


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, field: str | None = None, **details: object):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.details = details

    def to_dict(self) -> dict:
        error: dict = {"code": self.code, "message": self.message}
        if self.field is not None:
            error["field"] = self.field
        error.update(self.details)
        return {"error": error}


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def bad_request(code: str, message: str, *, field: str | None = None, **details: object) -> ApiError:
    return ApiError(400, code, message, field=field, **details)


def conflict(code: str, message: str, **details: object) -> ApiError:
    return ApiError(409, code, message, **details)
