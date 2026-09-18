from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import collect
from collect import TZ, History, Snapshot


def date_str(moment: datetime) -> str:
    offset = moment.utcoffset()
    sign = "-" if offset.total_seconds() < 0 else "+"
    minutes = abs(int(offset.total_seconds())) // 60
    return f"/Date({collect.to_ms(moment)}{sign}{minutes // 60:02d}{minutes % 60:02d})/"


def departure(sched: datetime, actual: datetime | None, trip: str = "t857-b7D4-slD", trip_id: int = 2135,
              last: bool = False) -> dict[str, Any]:
    return {
        "SDT": date_str(sched),
        "ADT": date_str(actual) if actual else None,
        "IsLastStopOnTrip": last,
        "Trip": {"GtfsTripId": trip, "TripId": trip_id},
    }


def snapshot(taken: datetime, departures: list[dict[str, Any]], vehicles: list[dict[str, Any]] | None = None,
             messages: list[dict[str, Any]] | None = None) -> Snapshot:
    return Snapshot(taken=taken, vehicles=vehicles or [], departures=departures,
                    timepoints=frozenset({165}), messages=messages or [], routes=frozenset({10, 11, 30}))


def at(hour: int, minute: int, second: int = 0, day: int = 17) -> datetime:
    return datetime(2026, 9, day, hour, minute, second, tzinfo=TZ)


def test_classify_uses_one_minute_early_and_five_minutes_late() -> None:
    assert collect.classify(-61) == "early"
    assert collect.classify(-60) == "onTime"
    assert collect.classify(300) == "onTime"
    assert collect.classify(301) == "late"


def test_block_of_splits_the_day_into_six_hour_blocks() -> None:
    assert collect.block_of(at(5, 59)) == "0"
    assert collect.block_of(at(6, 0)) == "6"
    assert collect.block_of(at(23, 59)) == "18"


def test_trip_start_handles_trips_that_cross_midnight() -> None:
    assert collect.trip_start(at(0, 10, day=18), 2350) == at(23, 50, day=17)
    assert collect.trip_start(at(21, 40), 2135) == at(21, 35)


def test_record_departures_counts_timepoints_once(tmp_path: Any) -> None:
    history = History(str(tmp_path))
    stop = {"StopId": 165, "RouteDirections": [{"RouteId": 11, "Departures": [
        departure(at(21, 35), at(21, 35, 30)),
        departure(at(21, 45), at(21, 52), trip="t861-b7EC-slD", trip_id=2145),
        departure(at(21, 55), None, trip="t873-b7EC-slD", trip_id=2155),
        departure(at(21, 50), at(21, 50), trip="t86E-b7EC-slD", trip_id=2150, last=True),
    ]}]}
    other = {"StopId": 999, "RouteDirections": [{"RouteId": 11, "Departures": [departure(at(21, 36), at(21, 40))]}]}
    snap = snapshot(at(21, 56), [stop, other])

    assert collect.record_departures(history, snap) == 2
    stats = history.day(at(21, 0))["blocks"]["18"]["routes"]["11"]
    assert stats["d"] == {"n": 2, "onTime": 1, "late": 1, "early": 0, "delaySum": 450, "delayMax": 420}
    assert stats["trips"] == ["t857-b7D4-slD", "t861-b7EC-slD"]
    assert collect.record_departures(history, snapshot(at(22, 6), [stop])) == 0


def test_record_vehicles_samples_each_bus(tmp_path: Any) -> None:
    history = History(str(tmp_path))
    buses = [
        {"RouteId": 30, "DisplayStatus": "Late", "Deviation": 6, "OnBoard": 78, "TotalCapacity": 66, "OccupancyStatus": 5},
        {"RouteId": 30, "DisplayStatus": "On Time", "Deviation": 0, "OnBoard": 22, "TotalCapacity": 50, "OccupancyStatus": 1},
        {"RouteId": 999, "DisplayStatus": "Late", "Deviation": 1, "OnBoard": 69, "TotalCapacity": 66, "OccupancyStatus": 5},
    ]
    collect.record_vehicles(history, snapshot(at(21, 23), [], vehicles=buses))
    blk = history.day(at(21, 23))["blocks"]["18"]
    v = blk["routes"]["30"]["v"]
    assert (blk["runs"], blk["busSum"], blk["busMax"]) == (1, 2, 2)
    assert (v["n"], v["late"], v["onSum"], v["onMax"], v["loadMax"]) == (2, 1, 100, 78, 1.182)
    assert v["occ"] == [0, 1, 0, 0, 0, 1, 0]


def test_record_alerts_keeps_only_alerts_in_effect_today(tmp_path: Any) -> None:
    history = History(str(tmp_path))
    messages = [
        {"MessageId": 1, "Header": "Detour", "FromDate": date_str(at(0, 0, day=1)), "ToDate": date_str(at(0, 0, day=30))},
        {"MessageId": 2, "Header": "Old", "FromDate": date_str(at(0, 0, day=1)), "ToDate": date_str(at(0, 0, day=16))},
    ]
    collect.record_alerts(history, snapshot(at(12, 0), [], messages=messages))
    assert list(history.day(at(12, 0))["alerts"]) == ["1"]


def test_save_writes_day_state_and_index(tmp_path: Any) -> None:
    history = History(str(tmp_path))
    collect.record_vehicles(history, snapshot(at(8, 0), [], vehicles=[{"RouteId": 10, "OnBoard": 4}]))
    history.save(collect.to_ms(at(8, 0)))
    index = json.loads((tmp_path / "index.json").read_text())
    assert index["days"][0]["date"] == "2026-09-17"
    assert index["days"][0]["busAvg"] == 1.0
    assert (tmp_path / "2026-09-17.json").exists()
    assert (tmp_path / "state.json").exists()
