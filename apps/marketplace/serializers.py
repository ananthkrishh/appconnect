from rest_framework import serializers

from .models import SUPPORTED_SCOPES, App, Installation


class AppSerializer(serializers.ModelSerializer):
    installed = serializers.SerializerMethodField()
    has_connector = serializers.SerializerMethodField()

    class Meta:
        model = App
        fields = [
            "slug",
            "name",
            "description",
            "developer",
            "homepage_url",
            "scopes",
            "events",
            "installed",
            "has_connector",
        ]

    def get_installed(self, app) -> bool:
        return any(i.status == Installation.Status.ACTIVE for i in app.installations.all())

    def get_has_connector(self, app) -> bool:
        return bool(app.connector_config)


class InstallRequestSerializer(serializers.Serializer):
    redirect_uri = serializers.URLField()
    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=SUPPORTED_SCOPES), required=False, allow_empty=False
    )
    state = serializers.CharField(required=False, allow_blank=True, max_length=200)
    code_challenge = serializers.CharField(required=False, allow_blank=True, max_length=128)
    code_challenge_method = serializers.ChoiceField(choices=["S256"], required=False)


class InstallationSerializer(serializers.ModelSerializer):
    app = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    installed_by = serializers.CharField(source="installed_by.get_username", read_only=True)

    class Meta:
        model = Installation
        fields = ["id", "app", "status", "scopes", "installed_by", "created_at", "activated_at", "uninstalled_at"]
        read_only_fields = fields


class InstallResponseSerializer(serializers.Serializer):
    installation = InstallationSerializer()
    redirect_url = serializers.URLField(help_text="Send the admin's browser here to complete the OAuth handshake.")
    webhook_secret = serializers.CharField(help_text="Shown once. Used by the app to verify webhook signatures.")


class TokenRequestSerializer(serializers.Serializer):
    grant_type = serializers.ChoiceField(choices=["authorization_code", "refresh_token"])
    client_id = serializers.CharField()
    client_secret = serializers.CharField()
    code = serializers.CharField(required=False)
    redirect_uri = serializers.CharField(required=False)
    code_verifier = serializers.CharField(required=False)
    refresh_token = serializers.CharField(required=False)


class TokenResponseSerializer(serializers.Serializer):
    access_token = serializers.CharField()
    token_type = serializers.CharField()
    expires_in = serializers.IntegerField()
    refresh_token = serializers.CharField()
    scope = serializers.CharField()
    installation_id = serializers.IntegerField()


class RevokeRequestSerializer(serializers.Serializer):
    client_id = serializers.CharField()
    client_secret = serializers.CharField()
    token = serializers.CharField()
