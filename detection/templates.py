"""Safe rendering of catalog text templates.

Templates use str.format syntax but resolve fields only by dict-key lookup
along dotted paths (`{worst.tags.switch}`) — no attribute access, no
indexing, no calls. A missing value renders as "unknown"; a format spec is
applied only when the value is numeric, so `{x:.2f}` on a missing field
can't raise.
"""

from __future__ import annotations

import string
from typing import Any

MISSING = "unknown"


class _SafeFormatter(string.Formatter):
    def get_field(self, field_name: str, args: Any, kwargs: Any) -> tuple[Any, str]:
        if "[" in field_name or "(" in field_name:
            raise ValueError(f"unsupported template field '{field_name}'")
        node: Any = kwargs
        for part in field_name.split("."):
            if isinstance(node, dict) and part in node and node[part] is not None:
                node = node[part]
            else:
                return MISSING, field_name
        return node, field_name

    def format_field(self, value: Any, format_spec: str) -> str:
        if format_spec and not (isinstance(value, (int, float)) and not isinstance(value, bool)):
            return str(value)
        return format(value, format_spec)

    def convert_field(self, value: Any, conversion: str | None) -> Any:
        return value  # !r / !s / !a are ignored


_FORMATTER = _SafeFormatter()


def render(template: str, context: dict[str, Any]) -> str:
    return _FORMATTER.vformat(template, (), context)
