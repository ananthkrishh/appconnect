from rest_framework.filters import BaseFilterBackend


class QueryParamFilterBackend(BaseFilterBackend):
    """Exact-match filters declared on the view as ``filter_fields``.

    ``filter_fields = {"status": "status", "department": "department__iexact"}``
    maps a query parameter to an ORM lookup. Comma-separated values become an
    ``__in`` lookup, so ``?status=active,on_leave`` works out of the box.
    """

    def filter_queryset(self, request, queryset, view):
        for param, lookup in getattr(view, "filter_fields", {}).items():
            raw = request.query_params.get(param)
            if raw in (None, ""):
                continue
            values = [v.strip() for v in raw.split(",") if v.strip()]
            if len(values) > 1 and "__" not in lookup:
                queryset = queryset.filter(**{f"{lookup}__in": values})
            else:
                queryset = queryset.filter(**{lookup: values[0]})
        return queryset

    def get_schema_operation_parameters(self, view):
        return [
            {
                "name": param,
                "in": "query",
                "required": False,
                "description": f"Filter by `{param}`. Comma-separate to match several values.",
                "schema": {"type": "string"},
            }
            for param in getattr(view, "filter_fields", {})
        ]
