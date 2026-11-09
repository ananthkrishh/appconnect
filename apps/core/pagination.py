from rest_framework import pagination


class PageNumberPagination(pagination.PageNumberPagination):
    """Offset pagination used by v1 (`?page=2&page_size=50`)."""

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


class CursorPagination(pagination.CursorPagination):
    """Cursor pagination used by v2: stable under concurrent inserts and O(1) per page."""

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
    ordering = "id"
