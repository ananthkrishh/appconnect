from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.pagination import CursorPagination, PageNumberPagination
from apps.core.permissions import IsStaffOrScopedApp
from apps.core.versioning import V1Versioning, V2Versioning
from apps.core.viewsets import BaseModelViewSet

from . import services
from .models import Employee
from .serializers import EmployeeV1Serializer, EmployeeV2Serializer, TerminateSerializer


class EmployeeViewSetBase(BaseModelViewSet):
    """Behaviour shared by every API version; subclasses only pick shape and paging."""

    queryset = Employee.objects.select_related("manager")
    permission_classes = [IsStaffOrScopedApp]
    required_scopes = {"read": "employees:read", "write": "employees:write"}
    filter_fields = {"status": "status", "department": "department__iexact", "employment_type": "employment_type"}
    search_fields = ("first_name", "last_name", "email")

    def perform_create(self, serializer):
        serializer.instance = services.hire_employee(**serializer.validated_data)

    def perform_update(self, serializer):
        serializer.instance = services.update_employee(serializer.instance, **serializer.validated_data)


@extend_schema_view(
    list=extend_schema(summary="List employees", deprecated=True),
    retrieve=extend_schema(summary="Get an employee", deprecated=True),
    create=extend_schema(summary="Create an employee", deprecated=True),
    partial_update=extend_schema(summary="Update an employee", deprecated=True),
)
class EmployeeV1ViewSet(EmployeeViewSetBase):
    """Employee records, v1 shape (deprecated: integer ids, page-number pagination)."""

    versioning_class = V1Versioning
    serializer_class = EmployeeV1Serializer
    pagination_class = PageNumberPagination
    lookup_field = "pk"
    ordering_fields = ("id", "last_name", "hire_date")


@extend_schema_view(
    list=extend_schema(summary="List employees"),
    retrieve=extend_schema(summary="Get an employee"),
    create=extend_schema(summary="Hire an employee", description="Publishes `employee.hired`."),
    partial_update=extend_schema(summary="Update an employee", description="Publishes `employee.updated`."),
)
class EmployeeV2ViewSet(EmployeeViewSetBase):
    """Employee records. Changes are pushed to installed apps as webhooks."""

    versioning_class = V2Versioning
    serializer_class = EmployeeV2Serializer
    pagination_class = CursorPagination
    lookup_field = "external_id"
    filter_fields = {**EmployeeViewSetBase.filter_fields, "updated_since": "updated_at__gte"}

    @extend_schema(
        summary="Terminate an employee",
        description="Sets status to `terminated` and publishes `employee.terminated`.",
        request=TerminateSerializer,
        responses={200: EmployeeV2Serializer},
    )
    @action(detail=True, methods=["post"])
    def terminate(self, request, *args, **kwargs):
        employee = self.get_object()
        body = TerminateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        employee = services.terminate_employee(employee, body.validated_data.get("termination_date"))
        return Response(EmployeeV2Serializer(employee).data, status=status.HTTP_200_OK)
