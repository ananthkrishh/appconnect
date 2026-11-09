"""Versioned employee serializers.

v1 is the original flat shape keyed by integer id with ``title``.
v2 uses the public UUID as ``id``, renames ``title`` to ``job_title``, adds
employment details and manager, and makes lifecycle fields read-only (status
changes go through ``POST /employees/{id}/terminate/``).
"""

from rest_framework import serializers

from .models import Employee


class EmployeeV1Serializer(serializers.ModelSerializer):
    title = serializers.CharField(source="job_title", required=False, allow_blank=True, max_length=150)

    class Meta:
        model = Employee
        fields = ["id", "first_name", "last_name", "email", "department", "title", "status", "hire_date"]
        read_only_fields = ["id"]

    def validate_status(self, value):
        if self.instance is None and value == Employee.Status.TERMINATED:
            raise serializers.ValidationError("New employees cannot be created as terminated.")
        return value


class EmployeeV2Serializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="external_id", read_only=True)
    full_name = serializers.CharField(read_only=True)
    manager_id = serializers.SlugRelatedField(
        source="manager",
        slug_field="external_id",
        queryset=Employee.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Employee
        fields = [
            "id",
            "first_name",
            "last_name",
            "full_name",
            "email",
            "department",
            "job_title",
            "employment_type",
            "status",
            "hire_date",
            "termination_date",
            "manager_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["status", "termination_date", "created_at", "updated_at"]

    def validate(self, attrs):
        manager = attrs.get("manager")
        if manager is not None and self.instance is not None and manager.pk == self.instance.pk:
            raise serializers.ValidationError({"manager_id": "An employee cannot manage themselves."})
        if manager is not None and manager.status == Employee.Status.TERMINATED:
            raise serializers.ValidationError({"manager_id": "Manager must be a current employee."})
        return attrs


class TerminateSerializer(serializers.Serializer):
    termination_date = serializers.DateField(required=False)
