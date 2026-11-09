"""Shared base viewsets.

New endpoints subclass one of these instead of DRF's viewsets directly so they
get the same lookup behaviour, filtering, ordering, pagination, error format and
deprecation headers without any per-endpoint code.
"""

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from rest_framework import mixins, viewsets
from rest_framework.exceptions import NotFound
from rest_framework.filters import OrderingFilter, SearchFilter

from .filters import QueryParamFilterBackend


class StandardViewSetMixin:
    filter_backends = [QueryParamFilterBackend, SearchFilter, OrderingFilter]
    filter_fields: dict[str, str] = {}
    search_fields: tuple[str, ...] = ()
    ordering_fields: tuple[str, ...] = ()
    lookup_url_kwarg = "id"

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        lookup_value = self.kwargs[self.lookup_url_kwarg]
        try:
            obj = get_object_or_404(queryset, **{self.lookup_field: lookup_value})
        except (ValueError, TypeError, DjangoValidationError):
            # Malformed ids (e.g. "abc" for an integer or UUID key) are a 404,
            # not a 400 or 500: the resource simply does not exist.
            raise NotFound() from None
        self.check_object_permissions(self.request, obj)
        return obj

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        sunset = settings.API_DEPRECATED_VERSIONS.get(getattr(request, "version", None))
        if sunset:
            response["Deprecation"] = "true"
            response["Sunset"] = sunset
            response["Link"] = '</api/v2/>; rel="successor-version"'
        return response


class BaseModelViewSet(StandardViewSetMixin, viewsets.ModelViewSet):
    """CRUD without hard deletes by default; subclasses opt in explicitly."""

    http_method_names = ["get", "post", "patch", "head", "options"]


class BaseReadOnlyViewSet(StandardViewSetMixin, viewsets.ReadOnlyModelViewSet):
    pass


class BaseReadDeleteViewSet(
    StandardViewSetMixin,
    mixins.DestroyModelMixin,
    viewsets.ReadOnlyModelViewSet,
):
    pass
