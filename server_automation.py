"""Schedule evaluation for the local preview server."""
from datetime import datetime


PERIODS = (15, 30, 60, 120, 240, 480, 1440)
FRESH_MS = 300_000


def decision(device, readings, now, previous_minute):
    schedule = device.get("schedule") or {}
    if not device.get("online") or not any(
        cap["id"] == "power" and cap["type"] == "switch" for cap in device["capabilities"]
    ):
        return None, None
    mode = schedule.get("mode")
    power, next_run = None, None
    if mode == "interval":
        period = schedule.get("period")
        period = (period if period in PERIODS else 60) * 60_000
        deadline = schedule.get("nextRun")
        if type(deadline) is not int or not 0 < deadline <= now + period:
            next_run = now + period
        elif now >= deadline:
            power, next_run = True, deadline + ((now - deadline) // period + 1) * period
    elif mode == "auto":
        metric = {"humidifier":"humidity", "purifier":"air_quality"}.get(device["kind"])
        if metric is None:
            return None, None
        source = next((item for item in readings if item["roomId"] == device["roomId"]
                       and item.get("online") and bool(item.get("transport")) == bool(device.get("transport"))
                       and any(cap["id"] == metric and cap["type"] == "sensor" for cap in item["capabilities"])
                       and type(item["state"].get(metric)) in (int, float) and 0 <= item["state"][metric] <= 100
                       and (not item.get("transport") or 0 <= now - item.get("lastSeen", 0) <= FRESH_MS)), None)
        if source:
            fallback = device["state"].get("target_humidity", 50) if metric == "humidity" else 85
            power = source["state"][metric] < schedule.get("threshold", fallback)
    elif mode is None and previous_minute != now // 60_000:
        local_time = datetime.fromtimestamp(now / 1000).strftime("%H:%M")
        if schedule.get("off") == local_time:
            power = False
        elif schedule.get("on") == local_time:
            power = True
    return None if power == device["state"].get("power") else power, next_run
