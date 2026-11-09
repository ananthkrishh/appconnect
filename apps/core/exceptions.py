"""One error format for every endpoint.

Every non-2xx response from the API looks like::

    {
      "error": {
        "code": "validation_error",
        "message": "Request validation failed.",
        "details": {"email": ["This field is required."]},
        "request_id": "3f0c..."
      }
    }

Clients only ever need one parser, regardless of API version or endpoint.
"""

import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class APIError(exceptions.APIException):
    """Base class for domain errors raised from services and views."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "bad_request"
    default_detail = "Bad request."

    def __init__(self, detail=None, code=None, details=None):
        super().__init__(detail=detail, code=code)
        self.details = details


class Conflict(APIError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"
    default_detail = "The request conflicts with the current state of the resource."


class InvalidStateTransition(Conflict):
    default_code = "invalid_state_transition"
    default_detail = "That state change is not allowed."


def error_body(code: str, message: str, details=None, request_id: str | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }


def _normalise(exc):
    if isinstance(exc, Http404):
        return exceptions.NotFound()
    if isinstance(exc, DjangoPermissionDenied):
        return exceptions.PermissionDenied()
    if isinstance(exc, DjangoValidationError):
        detail = exc.message_dict if hasattr(exc, "error_dict") else {"non_field_errors": exc.messages}
        return exceptions.ValidationError(detail)
    return exc


def exception_handler(exc, context):
    exc = _normalise(exc)
    request = context.get("request")
    request_id = getattr(request, "request_id", None) if request is not None else None

    response = drf_exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled API error", extra={"request_id": request_id})
        return Response(
            error_body("internal_error", "An unexpected error occurred.", request_id=request_id),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, exceptions.ValidationError):
        code = "validation_error"
        message = "Request validation failed."
        details = exc.detail if isinstance(exc.detail, dict) else {"non_field_errors": exc.detail}
    else:
        codes = exc.get_codes()
        code = codes if isinstance(codes, str) else getattr(exc, "default_code", "error")
        message = str(exc.detail)
        details = getattr(exc, "details", None)

    response.data = error_body(code, message, details, request_id)
    return response
