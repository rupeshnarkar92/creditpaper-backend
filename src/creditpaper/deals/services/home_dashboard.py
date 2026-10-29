"""Shape the single-call Home dashboard read for the API."""

from typing import Any

from creditpaper.deals.services import activity_presenter


def present_home_dashboard(raw: dict[str, Any]) -> dict[str, Any]:
    activity = raw.get("activity") or {}
    names = activity.get("names") or {}
    events = activity.get("events") or []
    return {
        "deals": raw.get("deals") or [],
        "activity": {
            "events": [
                activity_presenter.present(event, names) | {"deal": event["deal"]} for event in events
            ],
            "has_more": bool(activity.get("has_more")),
        },
    }
