from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.exceptions import Conflict
from apps.core.permissions import IsStaffUser
from apps.core.versioning import V2Versioning
from apps.core.viewsets import BaseReadOnlyViewSet

from .delivery import replay_deliveries
from .models import WebhookDelivery


class WebhookDeliverySerializer(serializers.ModelSerializer):
    app = serializers.CharField(source="installation.app.slug", read_only=True)

    class Meta:
        model = WebhookDelivery
        fields = [
            "id",
            "installation",
            "app",
            "event_id",
            "event_type",
            "idempotency_key",
            "status",
            "attempts",
            "replay_count",
            "next_attempt_at",
            "last_attempt_at",
            "last_status_code",
            "last_error",
            "delivered_at",
            "created_at",
            "payload",
        ]
        read_only_fields = fields


@extend_schema_view(
    list=extend_schema(summary="List webhook deliveries"),
    retrieve=extend_schema(summary="Get a webhook delivery"),
)
class WebhookDeliveryViewSet(BaseReadOnlyViewSet):
    """Delivery log for outbound webhooks, with manual replay."""

    versioning_class = V2Versioning
    serializer_class = WebhookDeliverySerializer
    permission_classes = [IsStaffUser]
    queryset = WebhookDelivery.objects.select_related("installation__app")
    filter_fields = {"status": "status", "event_type": "event_type", "installation": "installation_id"}
    lookup_field = "pk"

    @extend_schema(summary="Replay a failed delivery", request=None, responses={202: WebhookDeliverySerializer})
    @action(detail=True, methods=["post"])
    def replay(self, request, *args, **kwargs):
        delivery = self.get_object()
        if delivery.status != WebhookDelivery.Status.FAILED:
            raise Conflict("Only failed deliveries can be replayed.", code="not_replayable")
        if not replay_deliveries(since=None, limit=1, include_stale_pending=False, ids=[delivery.pk]):
            raise Conflict("The installation is no longer active.", code="not_replayable")
        delivery.refresh_from_db()
        return Response(self.get_serializer(delivery).data, status=202)
