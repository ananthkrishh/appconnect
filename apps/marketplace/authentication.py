from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

from .models import AccessToken, hash_secret


class AppPrincipal:
    """The `request.user` for calls made by an installed app with an OAuth token."""

    is_authenticated = True
    is_anonymous = False
    is_staff = False
    is_superuser = False

    def __init__(self, installation):
        self.installation = installation
        self.pk = None

    def __str__(self) -> str:
        return f"app:{self.installation.app.slug}"


class OAuthBearerAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid bearer token header.")

        raw = header[1].decode(errors="ignore")
        token = AccessToken.objects.select_related("installation__app").filter(token_hash=hash_secret(raw)).first()
        if token is None or not token.is_valid or not token.installation.is_active:
            raise exceptions.AuthenticationFailed("Access token is invalid, expired or revoked.")
        return AppPrincipal(token.installation), token

    def authenticate_header(self, request):
        return f'{self.keyword} realm="api"'


class OAuthBearerScheme(OpenApiAuthenticationExtension):
    target_class = "apps.marketplace.authentication.OAuthBearerAuthentication"
    name = "oauth2"

    def get_security_definition(self, auto_schema):
        return {
            "type": "oauth2",
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": "/api/v2/apps/{slug}/install/",
                    "tokenUrl": "/oauth/token/",
                    "scopes": {
                        "employees:read": "Read employee records",
                        "employees:write": "Create and update employee records",
                    },
                }
            },
        }
