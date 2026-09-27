"""MCP server exposing Garmin Connect data over stdio, or over HTTP when asked.

Every tool is read-only: nothing here writes to Garmin, and nothing returns the
password or the cached token.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Mapping

import anyio

try:
    from mcp.server.auth.middleware.auth_context import get_access_token
except ImportError:  # an mcp too old for HTTP auth, which stdio doesn't need
    def get_access_token():
        return None

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x called the same thing FastMCP
    from mcp.server.fastmcp import FastMCP as MCPServer

from . import remote
from .formatting import (
    DateError,
    drop_empty,
    duration,
    first_present,
    hr_zones,
    km,
    local_timestamp,
    minutes,
    pace_per_km,
    pace_per_mile,
    parse_date,
    rounded,
)
from .session import GarminError, session
from .workouts import SPORTS, WorkoutError, build_workout

log = logging.getLogger(__name__)

_server_options, _oauth_provider = remote.server_options()

mcp = MCPServer(
    "garmin",
    instructions=(
        "Read-only access to the user's own Garmin Connect account: daily health "
        "summaries, sleep, and activities. Dates are YYYY-MM-DD and also accept "
        "'today', 'yesterday', or a negative day offset such as '-7'. If a tool "
        "returns an 'error' key, show it to the user rather than retrying blindly."
    ),
    **_server_options,
)

MAX_ACTIVITIES = 50


async def _call(fn: Callable[[Any], Any]) -> Any:
    """Run a blocking Garmin call on a worker thread, reauthenticating if needed."""
    return await anyio.to_thread.run_sync(functools.partial(session.run, fn))


def tool_errors(fn):
    """Return a clear error payload instead of letting an exception escape.

    Anything unexpected is reported by type and message: enough for the user to
    act on, without a traceback going back through the transport.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        caller = get_access_token()
        if caller is not None:  # only over HTTP; stdio has no token
            log.info("%s called by %s", fn.__name__, caller.subject)
        try:
            return await fn(*args, **kwargs)
        except (GarminError, DateError, WorkoutError) as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a tool must never crash the server
            log.exception("Tool %s failed", fn.__name__)
            return {
                "error": (
                    f"Garmin request failed ({type(exc).__name__}: {exc}). "
                    "If this persists, check connection status with "
                    "get_connection_status."
                )
            }

    return wrapper


def _method(client: Any, *names: str) -> Callable[..., Any]:
    """Pick whichever name this version of garminconnect uses."""
    for name in names:
        fn = getattr(client, name, None)
        if callable(fn):
            return fn
    raise GarminError(
        f"Installed garminconnect has none of: {', '.join(names)}. "
        "Try upgrading it with `uv pip install -U garminconnect`."
    )


# --------------------------------------------------------------------------
# Daily summary
# --------------------------------------------------------------------------


@mcp.tool()
@tool_errors
async def get_daily_summary(date: str | None = None) -> dict[str, Any]:
    """Daily health summary: steps, calories, resting heart rate and body battery.

    Args:
        date: Day to report on. YYYY-MM-DD, 'today', 'yesterday' or an offset
            like '-3'. Defaults to today.
    """
    day = parse_date(date)
    stats = await _call(lambda c: _method(c, "get_stats", "get_user_summary")(day))
    if not stats:
        return {"date": day, "note": "Garmin returned no summary for this day."}

    return drop_empty(
        {
            "date": day,
            "steps": stats.get("totalSteps"),
            "step_goal": stats.get("dailyStepGoal"),
            "floors_climbed": stats.get("floorsAscended"),
            "distance_km": km(stats.get("totalDistanceMeters")),
            "calories": drop_empty(
                {
                    "total": stats.get("totalKilocalories"),
                    "active": stats.get("activeKilocalories"),
                    "resting_bmr": stats.get("bmrKilocalories"),
                }
            ),
            "heart_rate": drop_empty(
                {
                    "resting_bpm": stats.get("restingHeartRate"),
                    "min_bpm": stats.get("minHeartRate"),
                    "max_bpm": stats.get("maxHeartRate"),
                    "resting_7day_avg_bpm": stats.get(
                        "lastSevenDaysAvgRestingHeartRate"
                    ),
                }
            ),
            "body_battery": drop_empty(
                {
                    "most_recent": stats.get("bodyBatteryMostRecentValue"),
                    "highest": stats.get("bodyBatteryHighestValue"),
                    "lowest": stats.get("bodyBatteryLowestValue"),
                    "charged": stats.get("bodyBatteryChargedValue"),
                    "drained": stats.get("bodyBatteryDrainedValue"),
                    "gained_during_sleep": stats.get("bodyBatteryDuringSleep"),
                }
            ),
            "stress": drop_empty(
                {
                    "average": stats.get("averageStressLevel"),
                    "max": stats.get("maxStressLevel"),
                    "rest_minutes": minutes(stats.get("restStressDuration")),
                    "high_minutes": minutes(stats.get("highStressDuration")),
                }
            ),
            "intensity_minutes": drop_empty(
                {
                    "moderate": stats.get("moderateIntensityMinutes"),
                    "vigorous": stats.get("vigorousIntensityMinutes"),
                    "goal": stats.get("intensityMinutesGoal"),
                }
            ),
            "spo2_average": stats.get("averageSpo2"),
            "respiration_avg": stats.get("avgWakingRespirationValue"),
        }
    )


# --------------------------------------------------------------------------
# Sleep
# --------------------------------------------------------------------------


def _sleep_score(scores: dict[str, Any]) -> dict[str, Any]:
    overall = scores.get("overall") or {}
    qualifiers = {
        key: (value or {}).get("qualifierKey")
        for key, value in scores.items()
        if isinstance(value, dict) and key != "overall"
    }
    return drop_empty(
        {
            "overall": overall.get("value"),
            "rating": overall.get("qualifierKey"),
            "qualifiers": drop_empty(qualifiers),
        }
    )


@mcp.tool()
@tool_errors
async def get_sleep_data(date: str | None = None) -> dict[str, Any]:
    """Sleep stages and sleep score for a night.

    Args:
        date: The date you woke up on. YYYY-MM-DD, 'today', 'yesterday' or an
            offset like '-3'. Defaults to today.
    """
    day = parse_date(date)
    raw = await _call(lambda c: c.get_sleep_data(day))
    if not raw:
        return {"date": day, "note": "Garmin returned no sleep data for this night."}

    dto = raw.get("dailySleepDTO") or {}
    total = dto.get("sleepTimeSeconds")
    if not dto or total is None:
        return {
            "date": day,
            "note": "No sleep recorded for this night (watch not worn, or not synced).",
        }

    stages = {
        "deep": dto.get("deepSleepSeconds"),
        "light": dto.get("lightSleepSeconds"),
        "rem": dto.get("remSleepSeconds"),
        "awake": dto.get("awakeSleepSeconds"),
    }

    return drop_empty(
        {
            "date": day,
            "total_sleep": duration(total),
            "total_sleep_hours": rounded(total / 3600, 2),
            "asleep_at": local_timestamp(dto.get("sleepStartTimestampLocal")),
            "awake_at": local_timestamp(dto.get("sleepEndTimestampLocal")),
            "stages": drop_empty(
                {
                    name: drop_empty(
                        {
                            "time": duration(secs),
                            "minutes": minutes(secs),
                            "percent": (
                                round(secs / total * 100, 1)
                                if secs is not None and total
                                else None
                            ),
                        }
                    )
                    for name, secs in stages.items()
                }
            ),
            "score": _sleep_score(dto.get("sleepScores") or {}),
            "resting_heart_rate": raw.get("restingHeartRate"),
            "average_respiration": dto.get("averageRespirationValue"),
            "average_spo2": raw.get("averageSpO2Value"),
            "average_hrv_ms": raw.get("avgOvernightHrv"),
            "body_battery_change": raw.get("bodyBatteryChange"),
            "awake_count": raw.get("awakeCount"),
        }
    )


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------


def _running_dynamics(data: Mapping[str, Any]) -> dict[str, Any]:
    """Strap and watch dynamics, if the device recorded them.

    Field names differ between the activity list and the detail endpoint, so
    both spellings are tried. Everything here is absent for people without a
    compatible strap or watch, and drop_empty removes it rather than reporting
    a row of nulls.
    """
    return drop_empty(
        {
            "ground_contact_ms": rounded(
                first_present(data, "avgGroundContactTime", "groundContactTime"), 0
            ),
            "ground_contact_balance_left_pct": rounded(
                first_present(
                    data, "avgGroundContactBalance", "groundContactBalanceLeft"
                ),
                1,
            ),
            "vertical_oscillation_cm": rounded(
                first_present(data, "avgVerticalOscillation", "verticalOscillation"), 1
            ),
            "vertical_ratio_pct": rounded(
                first_present(data, "avgVerticalRatio", "verticalRatio"), 1
            ),
            "stride_length_cm": rounded(
                first_present(data, "avgStrideLength", "strideLength"), 1
            ),
        }
    )


def _power(data: Mapping[str, Any]) -> dict[str, Any]:
    return drop_empty(
        {
            "average_w": rounded(first_present(data, "avgPower", "averagePower"), 0),
            "max_w": rounded(data.get("maxPower"), 0),
            "normalized_w": rounded(
                first_present(data, "normPower", "normalizedPower"), 0
            ),
        }
    )


def _inline_hr_zones(activity: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Activity list rows often carry hrTimeInZone_1..5 already — use them free."""
    raw = [
        {"zoneNumber": n, "secsInZone": activity.get(f"hrTimeInZone_{n}")}
        for n in range(1, 6)
        if activity.get(f"hrTimeInZone_{n}") is not None
    ]
    return hr_zones(raw)


def _summarise_activity(activity: dict[str, Any]) -> dict[str, Any]:
    distance = activity.get("distance")
    secs = first_present(activity, "duration", "elapsedDuration", "movingDuration")
    return drop_empty(
        {
            "activity_id": activity.get("activityId"),
            "name": activity.get("activityName"),
            "type": (activity.get("activityType") or {}).get("typeKey"),
            "start_local": activity.get("startTimeLocal"),
            "location": activity.get("locationName"),
            "distance_km": km(distance),
            "duration": duration(secs),
            "duration_seconds": rounded(secs, 0),
            "moving_time": duration(activity.get("movingDuration")),
            "pace_per_km": pace_per_km(distance, activity.get("movingDuration") or secs),
            "pace_per_mile": pace_per_mile(
                distance, activity.get("movingDuration") or secs
            ),
            "avg_speed_kmh": rounded(
                (activity.get("averageSpeed") or 0) * 3.6 or None, 2
            ),
            "heart_rate": drop_empty(
                {
                    "average_bpm": rounded(activity.get("averageHR"), 0),
                    "max_bpm": rounded(activity.get("maxHR"), 0),
                }
            ),
            "hr_zones": _inline_hr_zones(activity),
            "calories": rounded(activity.get("calories"), 0),
            "elevation_gain_m": rounded(activity.get("elevationGain"), 0),
            "avg_cadence_spm": rounded(
                first_present(
                    activity,
                    "averageRunningCadenceInStepsPerMinute",
                    "averageBikingCadenceInRevPerMinute",
                ),
                0,
            ),
            "running_dynamics": _running_dynamics(activity),
            "power": _power(activity),
            "training_effect": drop_empty(
                {
                    "aerobic": rounded(activity.get("aerobicTrainingEffect"), 1),
                    "anaerobic": rounded(activity.get("anaerobicTrainingEffect"), 1),
                }
            ),
            "vo2max": rounded(activity.get("vO2MaxValue"), 1),
        }
    )


@mcp.tool()
@tool_errors
async def get_activities(
    limit: int = 10,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """List recent runs and workouts with distance, duration, pace and HR zones.

    Args:
        limit: Maximum activities to return (1-50). Defaults to 10.
        start_date: Optional first day of a date range, YYYY-MM-DD.
        end_date: Optional last day of a date range. Defaults to today when
            start_date is given.
    """
    limit = max(1, min(int(limit or 10), MAX_ACTIVITIES))

    if start_date:
        start = parse_date(start_date)
        end = parse_date(end_date) if end_date else parse_date("today")
        activities = await _call(lambda c: c.get_activities_by_date(start, end))
        window: dict[str, Any] = {"from": start, "to": end}
    else:
        activities = await _call(lambda c: c.get_activities(0, limit))
        window = {}

    activities = activities or []
    shown = [_summarise_activity(a) for a in activities[:limit]]
    return drop_empty(
        {
            "count": len(shown),
            "total_matching": len(activities) if start_date else None,
            "window": window or None,
            "truncated": len(activities) > len(shown) or None,
            "activities": shown,
            "note": (
                "HR zone detail is included when Garmin returns it on the list row; "
                "call get_activity_details for full zones and splits."
            ),
        }
    )


def _summarise_lap(lap: dict[str, Any], index: int) -> dict[str, Any]:
    distance = lap.get("distance")
    secs = first_present(lap, "duration", "movingDuration", "elapsedDuration")
    return drop_empty(
        {
            "split": lap.get("lapIndex") or index,
            "distance_km": km(distance),
            "duration": duration(secs),
            "pace_per_km": pace_per_km(distance, secs),
            "avg_hr": rounded(lap.get("averageHR"), 0),
            "max_hr": rounded(lap.get("maxHR"), 0),
            "elevation_gain_m": rounded(lap.get("elevationGain"), 0),
            "avg_cadence_spm": rounded(
                first_present(
                    lap,
                    "averageRunCadence",
                    "averageRunningCadenceInStepsPerMinute",
                    "averageBikingCadenceInRevPerMinute",
                ),
                0,
            ),
            "calories": rounded(lap.get("calories"), 0),
            "running_dynamics": _running_dynamics(lap),
            "power": _power(lap),
        }
    )


@mcp.tool()
@tool_errors
async def get_activity_details(activity_id: int | str) -> dict[str, Any]:
    """Splits and heart-rate detail for one activity.

    Args:
        activity_id: The activityId from get_activities.
    """
    try:
        activity_id = int(str(activity_id).strip())
    except ValueError:
        return {"error": f"activity_id must be numeric, got {activity_id!r}."}

    warnings: list[str] = []

    async def _optional(label: str, fn: Callable[[Any], Any]) -> Any:
        """One weak endpoint should degrade the response, not fail the tool."""
        try:
            return await _call(fn)
        except GarminError:
            raise
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{label} unavailable ({type(exc).__name__})")
            return None

    summary = await _optional(
        "summary",
        lambda c: _method(c, "get_activity", "get_activity_evaluation")(activity_id),
    )
    splits = await _optional("splits", lambda c: c.get_activity_splits(activity_id))
    zones = await _optional(
        "hr zones", lambda c: c.get_activity_hr_in_timezones(activity_id)
    )

    if summary is None and splits is None and zones is None:
        return {
            "error": (
                f"Garmin returned nothing for activity {activity_id}. Check the id "
                "from get_activities."
            )
        }

    summary = summary or {}
    laps = (splits or {}).get("lapDTOs") or []

    # The detail endpoint nests what the list endpoint keeps flat.
    flat = dict(summary)
    for key in ("summaryDTO", "activityTypeDTO"):
        nested = summary.get(key)
        if isinstance(nested, dict):
            flat.update(nested)
    if isinstance(summary.get("activityTypeDTO"), dict):
        flat["activityType"] = {"typeKey": summary["activityTypeDTO"].get("typeKey")}

    return drop_empty(
        {
            "activity_id": activity_id,
            "summary": _summarise_activity(flat) or None,
            "hr_zones": hr_zones(zones),
            "splits_count": len(laps) or None,
            "splits": [
                _summarise_lap(lap, i) for i, lap in enumerate(laps, start=1)
            ]
            or None,
            "warnings": warnings or None,
        }
    )


# --------------------------------------------------------------------------
# Workouts (additive only — these create and schedule, never delete or overwrite)
# --------------------------------------------------------------------------


@mcp.tool()
@tool_errors
async def list_workouts(limit: int = 20) -> dict[str, Any]:
    """List structured workouts saved in the Garmin account.

    Args:
        limit: Maximum workouts to return (1-100). Defaults to 20.
    """
    limit = max(1, min(int(limit or 20), 100))
    workouts = await _call(lambda c: c.get_workouts(0, limit)) or []
    return {
        "count": len(workouts),
        "workouts": [
            drop_empty(
                {
                    "workout_id": w.get("workoutId"),
                    "name": w.get("workoutName"),
                    "sport": (w.get("sportType") or {}).get("sportTypeKey"),
                    "estimated_duration": duration(w.get("estimatedDurationInSecs")),
                    "updated": w.get("updateDate"),
                }
            )
            for w in workouts
        ],
    }


@mcp.tool()
@tool_errors
async def create_workout(
    name: str,
    steps: list[dict[str, Any]],
    sport: str = "running",
    description: str | None = None,
) -> dict[str, Any]:
    """Create a structured workout in Garmin Connect.

    Adds a new workout; it never edits or replaces an existing one. Use
    schedule_workout afterwards to put it on a date so it syncs to the watch.

    Args:
        name: Name shown in Garmin Connect and on the watch.
        steps: Ordered list of steps. Each step is an object:
            - "type": warmup, interval, recovery, rest, cooldown, or repeat
            - exactly one of "duration_seconds" or "distance_meters"
            - optional target, either "pace" ("4:05", or ["4:00","4:10"] for a
              range, minutes per km) or "hr" ([150, 165] in bpm)
            A repeat looks like {"type": "repeat", "times": 5, "steps": [...]}
            and cannot contain another repeat.
            Example — 15 min warmup, 5x1km at 4:05 with 90s recoveries, 10 min
            cooldown:
                [{"type": "warmup", "duration_seconds": 900},
                 {"type": "repeat", "times": 5, "steps": [
                     {"type": "interval", "distance_meters": 1000, "pace": "4:05"},
                     {"type": "recovery", "duration_seconds": 90}]},
                 {"type": "cooldown", "duration_seconds": 600}]
        sport: running, cycling, swimming, walking or hiking. Defaults to running.
        description: Optional note stored with the workout.
    """
    workout, summary, estimated = build_workout(name, sport, steps, description)
    payload = workout.to_dict()

    result = await _call(lambda c: c.upload_workout(payload)) or {}
    workout_id = first_present(result, "workoutId", "id")
    if workout_id is None:
        return {
            "error": "Garmin accepted the request but returned no workout id.",
            "raw_response": str(result)[:300],
        }

    return {
        "workout_id": workout_id,
        "name": name,
        "sport": str(sport).lower(),
        "estimated_duration": duration(estimated),
        "summary": summary,
        "next_step": (
            "Call schedule_workout with this workout_id and a date to put it on "
            "the Garmin calendar so it reaches the watch."
        ),
    }


@mcp.tool()
@tool_errors
async def schedule_workout(workout_id: int | str, date: str) -> dict[str, Any]:
    """Put an existing workout on a date in the Garmin calendar.

    Scheduling is what makes a workout sync to the watch.

    Args:
        workout_id: Id from create_workout or list_workouts.
        date: The day to schedule it on. YYYY-MM-DD, 'today', 'tomorrow', or an
            offset like '+3'.
    """
    try:
        workout_id = int(str(workout_id).strip())
    except ValueError:
        return {"error": f"workout_id must be numeric, got {workout_id!r}."}

    day = parse_date(date)
    result = await _call(lambda c: c.schedule_workout(workout_id, day)) or {}
    return {
        "workout_id": workout_id,
        "scheduled_for": day,
        "schedule_id": first_present(result, "workoutScheduleId", "id"),
        "note": "Sync the watch (or open Garmin Connect on your phone) to pick it up.",
    }


# --------------------------------------------------------------------------
# Profile and personal records
# --------------------------------------------------------------------------

# Garmin identifies personal records by a numeric type. Only the running ones
# are labelled here; anything else is passed through with its raw id rather
# than guessed at.
RUNNING_RECORDS = {
    1: ("fastest_1k", "time"),
    2: ("fastest_1_mile", "time"),
    3: ("fastest_5k", "time"),
    4: ("fastest_10k", "time"),
    5: ("fastest_half_marathon", "time"),
    6: ("fastest_marathon", "time"),
    7: ("longest_run", "distance"),
}


@mcp.tool()
@tool_errors
async def get_profile() -> dict[str, Any]:
    """Fitness profile: VO2 max and personal records.

    Use this instead of asking the user for their PBs. Note that Garmin only
    knows records it has recorded itself — a race run without the watch, or
    before they owned it, will be missing.
    """
    warnings: list[str] = []

    async def _optional(label: str, fn: Callable[[Any], Any]) -> Any:
        try:
            return await _call(fn)
        except GarminError:
            raise
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{label} unavailable ({type(exc).__name__})")
            return None

    today = parse_date("today")
    metrics = await _optional("vo2 max", lambda c: c.get_max_metrics(today))
    records = await _optional("personal records", lambda c: c.get_personal_record())

    vo2, fitness_age = None, None
    if isinstance(metrics, list) and metrics:
        generic = (metrics[0] or {}).get("generic") or {}
        vo2 = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
        fitness_age = generic.get("fitnessAge")

    running: dict[str, Any] = {}
    other: list[dict[str, Any]] = []
    for record in records or []:
        type_id = record.get("typeId")
        value = record.get("value")
        if value is None:
            continue
        known = RUNNING_RECORDS.get(type_id)
        if known and record.get("activityType") == "running":
            key, kind = known
            running[key] = (
                duration(value) if kind == "time" else f"{km(value)} km"
            )
        else:
            other.append(
                drop_empty(
                    {
                        "type_id": type_id,
                        "activity_type": record.get("activityType"),
                        "value": rounded(value, 1),
                    }
                )
            )

    return drop_empty(
        {
            "vo2max": rounded(vo2, 1),
            "fitness_age": fitness_age,
            "running_records": running or None,
            "other_records": other or None,
            "note": (
                "Personal records only cover activities recorded on the watch. "
                "Garmin's heart-rate zones depend on a max heart rate the user "
                "sets in their profile, which is often an age-based estimate — "
                "ask them to confirm it before leaning on zone percentages."
            ),
            "warnings": warnings or None,
        }
    )


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


@mcp.tool()
@tool_errors
async def get_connection_status() -> dict[str, Any]:
    """Check whether the server is logged in to Garmin Connect.

    Reports which credentials are present and whether the cached session is
    usable. Never returns the password or the cached token itself.
    """
    return await anyio.to_thread.run_sync(session.status)


def main() -> None:
    # stdout belongs to the MCP protocol; every log line goes to stderr.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if remote.transport() == "http":
        remote.run(mcp, _oauth_provider)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
