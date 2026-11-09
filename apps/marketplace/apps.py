from django.apps import AppConfig


class MarketplaceConfig(AppConfig):
    name = "apps.marketplace"
    label = "marketplace"

    def ready(self):
        from . import authentication  # noqa: F401  registers the OpenAPI auth extension
