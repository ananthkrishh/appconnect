import re
import uuid

_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestIDMiddleware:
    """Attach a request id to every request and echo it back as `X-Request-ID`.

    A caller-supplied id is kept if it looks sane so traces can be correlated
    across services; otherwise a fresh UUID is generated.
    """

    header = "HTTP_X_REQUEST_ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get(self.header, "")
        request.request_id = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        return response
