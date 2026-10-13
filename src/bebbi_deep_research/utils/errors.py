"""API errors in the backend-common format (`errorId`, `status`, `debugMessage`).

`debugMessage` carries the German text that the pipe shows to the user as it is.
"""

from dcc_backend_common.fastapi_error_handling import ApiErrorCodes, ApiErrorException, api_error_exception
from fastapi import status


def not_found(message: str) -> ApiErrorException:
    """Return a 404 error (unknown or foreign draft or job)."""
    return api_error_exception(ApiErrorCodes.RESOURCE_NOT_FOUND, status.HTTP_404_NOT_FOUND, message)


def invalid_request(message: str) -> ApiErrorException:
    """Return a 400 error (unknown preset or depth)."""
    return api_error_exception(ApiErrorCodes.INVALID_REQUEST, status.HTTP_400_BAD_REQUEST, message)


def user_error(message: str) -> ApiErrorException:
    """Return a 422 error whose message is meant for the chat user as it is."""
    return api_error_exception(ApiErrorCodes.VALIDATION_ERROR, status.HTTP_422_UNPROCESSABLE_CONTENT, message)


def conflict(message: str) -> ApiErrorException:
    """Return a 409 error (no report yet)."""
    return api_error_exception(ApiErrorCodes.INVALID_REQUEST, status.HTTP_409_CONFLICT, message)


def rate_limited(message: str) -> ApiErrorException:
    """Return a 429 error (daily quota reached)."""
    return api_error_exception(ApiErrorCodes.RATE_LIMIT_EXCEEDED, status.HTTP_429_TOO_MANY_REQUESTS, message)


def unauthorized() -> ApiErrorException:
    """Return a 401 error (missing or wrong bearer token)."""
    return api_error_exception(ApiErrorCodes.AUTHENTICATION_FAILED, status.HTTP_401_UNAUTHORIZED, "invalid token")
