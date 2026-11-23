"""Current local time in one or more IANA time zones."""

from datetime import datetime, timezone
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import Tool


class CurrentTimeTool(Tool):
    name = "get_current_time"
    description = (
        "Get the current time in one or more IANA time zones. Convert city names to "
        "zone IDs and clarify ambiguous abbreviations. Default: Beijing (Asia/Shanghai)."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "timezones": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IANA zone IDs, e.g. Asia/Shanghai and America/New_York.",
            },
        },
        "required": [],
    }

    def execute(self, timezones: list[str] | None = None) -> str:
        if timezones is not None and not isinstance(timezones, list):
            return "Error: timezones must be an array of IANA zone IDs"

        now = datetime.now(timezone.utc)
        lines = []
        for name in timezones or ["Asia/Shanghai"]:
            try:
                if not isinstance(name, str):
                    raise ValueError
                local = now.astimezone(ZoneInfo(name))
            except (ValueError, ZoneInfoNotFoundError):
                lines.append(f"{name!r}: invalid or unknown time zone")
                continue
            stamp = local.isoformat(sep=" ", timespec="seconds")
            lines.append(f"{name} → {stamp[:19]} {stamp[19:]}")
        return "\n".join(lines)
