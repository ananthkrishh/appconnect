from django.db.models import Prefetch
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.exceptions import APIError
from apps.core.permissions import IsStaffUser
from apps.core.versioning import V2Versioning
from apps.core.viewsets import BaseReadDeleteViewSet, BaseReadOnlyViewSet

from . import oauth
from .models import App, Installation
from .serializers import (
    AppSerializer,
    InstallationSerializer,
    InstallRequestSerializer,
    InstallResponseSerializer,
    RevokeRequestSerializer,
    TokenRequestSerializer,
    TokenResponseSerializer,
)


@extend_schema_view(
    list=extend_schema(summary="Browse the app marketplace"),
    retrieve=extend_schema(summary="Get an app listing"),
)
class AppViewSet(BaseReadOnlyViewSet):
    """Third-party apps available to install."""

    versioning_class = V2Versioning
    serializer_class = AppSerializer
    permission_classes = [IsStaffUser]
    lookup_field = "slug"
    search_fields = ("name", "description", "developer")
    ordering_fields = ("name", "created_at")

    def get_queryset(self):
        return App.objects.filter(is_published=True).prefetch_related(
            Prefetch("installations", queryset=Installation.objects.filter(status=Installation.Status.ACTIVE))
        )

    @extend_schema(
        summary="Install an app",
        description=(
            "Admin consent step of the OAuth 2.0 authorization code flow. Creates a pending "
            "installation and returns the app's redirect URL carrying a one-time authorization "
            "code. The installation becomes active once the app exchanges the code at `/oauth/token/`."
        ),
        request=InstallRequestSerializer,
        responses={201: InstallResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def install(self, request, *args, **kwargs):
        app = self.get_object()
        body = InstallRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            installation, redirect_url = oauth.start_install(app, request.user, **body.validated_data)
        except oauth.OAuthError as exc:
            raise APIError(exc.description, code=exc.error) from None
        data = {
            "installation": InstallationSerializer(installation).data,
            "redirect_url": redirect_url,
            "webhook_secret": installation.webhook_secret,
        }
        return Response(data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(summary="List installations"),
    retrieve=extend_schema(summary="Get an installation"),
    destroy=extend_schema(
        summary="Uninstall an app",
        description="Revokes the app's tokens, cancels pending webhooks and sends `app.uninstalled`.",
    ),
)
class InstallationViewSet(BaseReadDeleteViewSet):
    """Apps installed in this organisation."""

    versioning_class = V2Versioning
    serializer_class = InstallationSerializer
    permission_classes = [IsStaffUser]
    queryset = Installation.objects.select_related("app", "installed_by")
    filter_fields = {"status": "status", "app": "app__slug"}
    lookup_field = "pk"

    def perform_destroy(self, instance):
        oauth.uninstall(instance)


class _OAuthEndpoint(APIView):
    """OAuth endpoints use client credentials, not user auth, and RFC 6749 error bodies."""

    versioning_class = V2Versioning  # unversioned URL, documented with v2
    authentication_classes: list = []
    permission_classes = [AllowAny]
    parser_classes = [FormParser, JSONParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "oauth"

    @staticmethod
    def oauth_error(exc: oauth.OAuthError) -> Response:
        response = Response({"error": exc.error, "error_description": exc.description}, status=exc.status)
        if exc.status == 401:
            response["WWW-Authenticate"] = 'Basic realm="oauth"'
        return response

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class TokenView(_OAuthEndpoint):
    """OAuth 2.0 token endpoint (RFC 6749). Accepts form or JSON bodies."""

    @extend_schema(
        summary="Exchange an authorization code or refresh token",
        request=TokenRequestSerializer,
        responses={200: TokenResponseSerializer, 400: OpenApiResponse(description="RFC 6749 error")},
        tags=["oauth"],
    )
    def post(self, request):
        body = TokenRequestSerializer(data=request.data)
        if not body.is_valid():
            return self.oauth_error(oauth.OAuthError("invalid_request", "Missing or invalid parameters."))
        data = body.validated_data
        try:
            if data["grant_type"] == "authorization_code":
                pair = oauth.exchange_code(
                    client_id=data["client_id"],
                    client_secret=data["client_secret"],
                    code=data.get("code", ""),
                    redirect_uri=data.get("redirect_uri", ""),
                    code_verifier=data.get("code_verifier", ""),
                )
            else:
                pair = oauth.refresh(
                    client_id=data["client_id"],
                    client_secret=data["client_secret"],
                    refresh_token=data.get("refresh_token", ""),
                )
        except oauth.OAuthError as exc:
            return self.oauth_error(exc)
        return Response(pair.as_response())


class RevokeView(_OAuthEndpoint):
    """OAuth 2.0 token revocation (RFC 7009)."""

    @extend_schema(
        summary="Revoke an access or refresh token",
        request=RevokeRequestSerializer,
        responses={200: OpenApiResponse(description="Token revoked (or was already invalid)")},
        tags=["oauth"],
    )
    def post(self, request):
        body = RevokeRequestSerializer(data=request.data)
        if not body.is_valid():
            return self.oauth_error(oauth.OAuthError("invalid_request", "Missing or invalid parameters."))
        try:
            oauth.revoke(**body.validated_data)
        except oauth.OAuthError as exc:
            return self.oauth_error(exc)
        return Response(status=status.HTTP_200_OK)
