"""Per-endpoint version pinning.

All endpoints use URL path versioning (``/api/v1/...``, ``/api/v2/...``). Pinning
the allowed versions on each viewset means each version's OpenAPI document only
lists the endpoints that version really has, and a viewset can never be served
under a version it was not written for.
"""

from rest_framework import exceptions
from rest_framework.versioning import URLPathVersioning


class PinnedURLPathVersioning(URLPathVersioning):
    def determine_version(self, request, *args, **kwargs):
        version = kwargs.get(self.version_param, self.default_version)
        if version not in self.allowed_versions:
            # URL routing already restricts versions at runtime, so this only fires
            # during schema generation, where drf-spectacular treats NotAcceptable
            # as "this endpoint is not part of the requested version".
            raise exceptions.NotAcceptable(self.invalid_version_message)
        return version


class V1Versioning(PinnedURLPathVersioning):
    allowed_versions = ("v1",)
    default_version = "v1"


class V2Versioning(PinnedURLPathVersioning):
    allowed_versions = ("v2",)
    default_version = "v2"
