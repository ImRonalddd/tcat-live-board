#!/usr/bin/env python3
"""Record one snapshot of TCAT service into per-day history files.

A GitHub Action runs this every 10 minutes. The TCAT API keeps actual
departure times for only about 15 minutes, so each run saves the
departures that finished since the previous run.

Usage: python3 collect.py OUTPUT_DIR
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

BASE = "https://realtimetcatbus.availtec.com/InfoPoint/rest"
TZ = ZoneInfo("America/New_York")
USER_AGENT = "tcat-live-board history recorder (github.com/ImRonalddd/tcat-live-board)"
EARLY_S = -60  # leaving more than 1 minute early counts as early
LATE_S = 300  # leaving more than 5 minutes late counts as late
KEEP_KEYS_MS = 90 * 60 * 1000  # remember recorded departures this long to skip repeats


class Snapshot(NamedTuple):
    taken: datetime
    vehicles: list[dict[str, Any]]
    departures: list[dict[str, Any]]
    timepoints: frozenset[int]
    messages: list[dict[str, Any]]
    routes: frozenset[int]  # public routes, so deadhead buses heading to the garage are skipped


def fetch(path: str) -> Any:
    request = urllib.request.Request(BASE + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def take_snapshot() -> Snapshot:
    stops = fetch("/Stops/GetAllStops")
    return Snapshot(
        taken=datetime.now(TZ),
        vehicles=fetch("/Vehicles/GetAllVehicles"),
        departures=fetch("/StopDepartures/GetAllStopDeparturesWithPastDepartures"),
        timepoints=frozenset(s["StopId"] for s in stops if s.get("IsTimePoint")),
        messages=fetch("/PublicMessages/GetAllMessages"),
        routes=frozenset(r["RouteId"] for r in fetch("/Routes/GetVisibleRoutes")),
    )


def ms(value: str | None) -> int | None:
    """Parse '/Date(1789702260000-0400)/' into epoch milliseconds."""
    match = re.match(r"/Date\((-?\d+)", value or "")
    return int(match.group(1)) if match else None


def local(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, TZ)


def to_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def block_of(moment: datetime) -> str:
    """Name the 6-hour block that holds a local time: '0', '6', '12', or '18'."""
    return str(moment.hour // 6 * 6)


def classify(delay_s: int) -> str:
    if delay_s < EARLY_S:
        return "early"
    if delay_s > LATE_S:
        return "late"
    return "onTime"


def trip_start(departure: datetime, trip_id: int) -> datetime:
    """Return when a trip started. TCAT's TripId is the start time as HHMM."""
    hour, minute = divmod(trip_id, 100)
    start = departure.replace(hour=hour % 24, minute=minute % 60, second=0, microsecond=0)
    if start > departure:  # the trip started before midnight
        start -= timedelta(days=1)
    return start


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_json(path: str, data: Any) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"), sort_keys=True)
    os.replace(temporary, path)


def new_day(date: str) -> dict[str, Any]:
    return {"date": date, "updated": 0, "blocks": {}, "alerts": {}}


def block(day: dict[str, Any], name: str) -> dict[str, Any]:
    return day["blocks"].setdefault(name, {"runs": 0, "busSum": 0, "busMax": 0, "routes": {}})


def route_stats(blk: dict[str, Any], route_id: int) -> dict[str, Any]:
    return blk["routes"].setdefault(str(route_id), {
        "v": {"n": 0, "late": 0, "early": 0, "devSum": 0, "devMax": 0,
              "onSum": 0, "onMax": 0, "capSum": 0, "loadMax": 0, "occ": [0] * 7},
        "d": {"n": 0, "onTime": 0, "late": 0, "early": 0, "delaySum": 0, "delayMax": 0},
        "trips": [],
    })


def summarize(day: dict[str, Any]) -> dict[str, Any]:
    """Whole-day totals for the index that lists every recorded day."""
    runs = buses = samples = late = riders = deps = on_time = delay = trips = 0
    for blk in day["blocks"].values():
        runs += blk["runs"]
        buses += blk["busSum"]
        for stats in blk["routes"].values():
            v, d = stats["v"], stats["d"]
            samples += v["n"]
            late += v["late"]
            riders += v["onSum"]
            deps += d["n"]
            on_time += d["onTime"]
            delay += d["delaySum"]
            trips += len(stats["trips"])
    return {
        "date": day["date"],
        "runs": runs,
        "busAvg": round(buses / runs, 1) if runs else None,
        "lateShare": round(late / samples, 3) if samples else None,
        "ridersAvg": round(riders / samples, 1) if samples else None,
        "departures": deps,
        "onTime": round(on_time / deps, 3) if deps else None,
        "avgDelay": round(delay / deps) if deps else None,
        "trips": trips,
        "alerts": len(day["alerts"]),
    }


class History:
    """Per-day JSON files, an index of all days, and a small state file."""

    def __init__(self, folder: str) -> None:
        self.folder = folder
        self.days: dict[str, dict[str, Any]] = {}
        self.state = read_json(os.path.join(folder, "state.json"), {"recent": {}})

    def path(self, name: str) -> str:
        return os.path.join(self.folder, f"{name}.json")

    def day(self, moment: datetime) -> dict[str, Any]:
        date = moment.strftime("%Y-%m-%d")
        if date not in self.days:
            self.days[date] = read_json(self.path(date), None) or new_day(date)
        return self.days[date]

    def save(self, now_ms: int) -> None:
        for date, day in self.days.items():
            day["updated"] = now_ms
            write_json(self.path(date), day)
        write_json(self.path("state"), self.state)
        index = read_json(self.path("index"), {"days": []})
        entries = {entry["date"]: entry for entry in index["days"]}
        entries.update({date: summarize(day) for date, day in self.days.items()})
        write_json(self.path("index"), {"updated": now_ms, "days": sorted(entries.values(), key=lambda e: e["date"])})


def record_vehicles(history: History, snap: Snapshot) -> None:
    """Count one sample per bus in service, in the block of the snapshot time."""
    in_service = [v for v in snap.vehicles if v.get("RouteId") in snap.routes]
    blk = block(history.day(snap.taken), block_of(snap.taken))
    blk["runs"] += 1
    blk["busSum"] += len(in_service)
    blk["busMax"] = max(blk["busMax"], len(in_service))
    for vehicle in in_service:
        v = route_stats(blk, vehicle["RouteId"])["v"]
        status = vehicle.get("DisplayStatus")
        deviation = vehicle.get("Deviation") or 0
        on_board = vehicle.get("OnBoard") or 0
        capacity = vehicle.get("TotalCapacity") or 0
        occupancy = vehicle.get("OccupancyStatus")
        v["n"] += 1
        v["late"] += int(status == "Late")
        v["early"] += int(status == "Early")
        v["devSum"] += deviation
        v["devMax"] = max(v["devMax"], deviation)
        v["onSum"] += on_board
        v["onMax"] = max(v["onMax"], on_board)
        v["capSum"] += capacity
        if capacity:
            v["loadMax"] = max(v["loadMax"], round(on_board / capacity, 3))
        if isinstance(occupancy, int) and 0 <= occupancy < len(v["occ"]):
            v["occ"][occupancy] += 1


def record_departures(history: History, snap: Snapshot) -> int:
    """Save timepoint departures that have an actual time. Return how many were new."""
    now_ms = to_ms(snap.taken)
    recent = {key: seen for key, seen in history.state["recent"].items() if now_ms - seen < KEEP_KEYS_MS}
    added = 0
    for stop in snap.departures:
        if stop["StopId"] not in snap.timepoints:
            continue
        for direction in stop.get("RouteDirections") or []:
            for dep in direction.get("Departures") or []:
                actual, scheduled = ms(dep.get("ADT")), ms(dep.get("SDT"))
                trip = dep.get("Trip") or {}
                gtfs_id = trip.get("GtfsTripId")
                if not actual or not scheduled or not gtfs_id or dep.get("IsLastStopOnTrip"):
                    continue
                key = f"{gtfs_id}|{stop['StopId']}|{scheduled}"
                if key in recent:
                    continue
                recent[key] = now_ms
                added += 1
                when = local(scheduled)
                delay = (actual - scheduled) // 1000
                d = route_stats(block(history.day(when), block_of(when)), direction["RouteId"])["d"]
                d["n"] += 1
                d[classify(delay)] += 1
                d["delaySum"] += delay
                d["delayMax"] = max(d["delayMax"], delay)
                if isinstance(trip.get("TripId"), int):
                    start = trip_start(when, trip["TripId"])
                    seen = route_stats(block(history.day(start), block_of(start)), direction["RouteId"])["trips"]
                    if gtfs_id not in seen:
                        seen.append(gtfs_id)
    history.state["recent"] = recent
    return added


def record_alerts(history: History, snap: Snapshot) -> None:
    """List every alert whose date range covers the snapshot's day."""
    today = snap.taken.date()
    alerts = history.day(snap.taken)["alerts"]
    for message in snap.messages:
        start, end = ms(message.get("FromDate")), ms(message.get("ToDate"))
        if start is None or end is None or not local(start).date() <= today <= local(end).date():
            continue
        entry = alerts.setdefault(str(message.get("MessageId")), {"first": to_ms(snap.taken)})
        entry.update({
            "header": message.get("Header") or "",
            "message": message.get("Message") or "",
            "cause": message.get("CauseReportLabel") or "",
            "effect": message.get("EffectReportLabel") or "",
            "priority": message.get("Priority"),
            "from": start,
            "to": end,
        })


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    os.makedirs(argv[1], exist_ok=True)
    try:
        snap = take_snapshot()
    except OSError as error:  # a short TCAT outage: skip this run instead of failing the job
        print(f"Skipped: the TCAT API did not answer ({error})")
        return 0
    history = History(argv[1])
    record_vehicles(history, snap)
    added = record_departures(history, snap)
    record_alerts(history, snap)
    history.save(to_ms(snap.taken))
    print(f"{snap.taken:%Y-%m-%d %H:%M %Z}: {len(snap.vehicles)} buses, {added} new timepoint departures")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
