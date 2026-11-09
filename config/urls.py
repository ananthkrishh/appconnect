from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from apps.core.views import healthz
from apps.employees.views import EmployeeV1ViewSet, EmployeeV2ViewSet
from apps.marketplace.views import AppViewSet, InstallationViewSet, RevokeView, TokenView
from apps.webhooks.views import WebhookDeliveryViewSet


def docs_urls():
    return [
        path("schema/", SpectacularAPIView.as_view(), name="schema"),
        path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
        path("redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    ]


# v1: original employee API, frozen and deprecated.
v1 = DefaultRouter(trailing_slash=True)
v1.include_root_view = False
v1.register("employees", EmployeeV1ViewSet, basename="employee-v1")

# v2: current API, including the marketplace.
v2 = DefaultRouter(trailing_slash=True)
v2.include_root_view = False
v2.register("employees", EmployeeV2ViewSet, basename="employee-v2")
v2.register("apps", AppViewSet, basename="app")
v2.register("installations", InstallationViewSet, basename="installation")
v2.register("webhook-deliveries", WebhookDeliveryViewSet, basename="webhook-delivery")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="healthz"),
    path("oauth/token/", TokenView.as_view(), name="oauth-token"),
    path("oauth/revoke/", RevokeView.as_view(), name="oauth-revoke"),
    # Literal prefixes keep each version's routes distinct; the `version` kwarg feeds URLPathVersioning.
    path("api/v1/", include(v1.urls + docs_urls()), {"version": "v1"}),
    path("api/v2/", include(v2.urls + docs_urls()), {"version": "v2"}),
]

handler404 = "apps.core.views.not_found"
handler500 = "apps.core.views.server_error"
