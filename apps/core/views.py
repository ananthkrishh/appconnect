from django.db import connection
from django.http import JsonResponse

from .exceptions import error_body


def healthz(request):
    """Liveness + database check for load balancers and docker compose."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ok = True
    except Exception:  # pragma: no cover - only hit when the DB is down
        db_ok = False
    return JsonResponse({"status": "ok" if db_ok else "degraded", "database": db_ok}, status=200 if db_ok else 503)


def not_found(request, exception=None):
    """JSON 404 for unmatched URLs so API clients never get an HTML page."""
    return JsonResponse(
        error_body("not_found", "Not found.", request_id=getattr(request, "request_id", None)),
        status=404,
    )


def server_error(request):
    return JsonResponse(
        error_body("internal_error", "An unexpected error occurred.", request_id=getattr(request, "request_id", None)),
        status=500,
    )
