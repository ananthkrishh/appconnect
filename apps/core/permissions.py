from rest_framework.permissions import SAFE_METHODS, BasePermission


def is_app_request(request) -> bool:
    """True when the caller authenticated with an OAuth access token issued to an installed app."""
    return getattr(request.auth, "is_app_token", False)


class IsStaffOrScopedApp(BasePermission):
    """Staff users get full access; installed apps need the right OAuth scope.

    Views declare ``required_scopes = {"read": "employees:read", "write": "employees:write"}``.
    """

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        if is_app_request(request):
            scopes = getattr(view, "required_scopes", {})
            needed = scopes.get("read" if request.method in SAFE_METHODS else "write")
            if needed is None:
                return False
            if needed not in request.auth.scopes:
                self.message = f"Access token is missing the '{needed}' scope."
                return False
            return True
        user = request.user
        return bool(user and user.is_authenticated and user.is_staff)


class IsStaffUser(BasePermission):
    """Staff only. App tokens are never allowed (e.g. an app cannot install other apps)."""

    def has_permission(self, request, view):
        if is_app_request(request):
            return False
        user = request.user
        return bool(user and user.is_authenticated and user.is_staff)
