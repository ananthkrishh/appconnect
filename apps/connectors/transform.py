"""Apply a connector config to a canonical employee payload at runtime."""

import datetime as dt


def _transform(value, field: dict):
    name = field.get("transform", "none")
    if name == "none" or (value is None and name != "is_active"):
        return value
    if name == "lowercase":
        return str(value).lower()
    if name == "uppercase":
        return str(value).upper()
    if name == "iso_date":
        return dt.date.fromisoformat(str(value)).isoformat()
    if name == "us_date":
        return dt.date.fromisoformat(str(value)).strftime("%m/%d/%Y")
    if name == "is_active":
        return value == "active"
    if name == "map":
        return field.get("values", {}).get(str(value), value)
    raise ValueError(f"Unknown transform: {name}")


def _set_path(target: dict, path: str, value) -> None:
    *parents, leaf = path.split(".")
    node = target
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value


def apply_mapping(config: dict, employee: dict) -> dict:
    result: dict = {}
    for field in config["fields"]:
        value = field["constant"] if "constant" in field else _transform(employee.get(field["source"]), field)
        _set_path(result, field["target"], value)
    return result
