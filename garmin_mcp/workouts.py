"""Build Garmin structured workouts from a plain step description.

Garmin's workout JSON is deeply nested and full of magic ids. This module turns
a simple list of steps — the kind you would describe out loud — into the models
garminconnect uploads, and renders one back as text so you can check it before
it lands in your account.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from garminconnect import workout as gw

# Garmin's own workoutTargetTypeId values. garminconnect's TargetType has never
# had a pace entry and has renumbered the rest between releases, so these are
# pinned here rather than borrowed from it.
TARGET_NONE = 1
TARGET_HEART_RATE_ZONE = 4
TARGET_PACE_ZONE = 6

# Sport name -> (workout model, default pace seconds per km used for estimates)
SPORTS: dict[str, tuple[type, float]] = {
    "running": (gw.RunningWorkout, 300.0),
    "cycling": (gw.CyclingWorkout, 120.0),
    "swimming": (gw.SwimmingWorkout, 1500.0),
    "walking": (gw.WalkingWorkout, 720.0),
    "hiking": (gw.HikingWorkout, 900.0),
}

STEP_TYPES = {
    "warmup": (gw.StepType.WARMUP, "warmup", 1),
    "cooldown": (gw.StepType.COOLDOWN, "cooldown", 2),
    "interval": (gw.StepType.INTERVAL, "interval", 3),
    "recovery": (gw.StepType.RECOVERY, "recovery", 4),
    "rest": (gw.StepType.REST, "rest", 5),
}

# A single pace is widened into a window this many seconds per km either side,
# because Garmin alerts on a range and an exact target would beep constantly.
PACE_WINDOW_SECONDS = 5.0


class WorkoutError(ValueError):
    """Bad workout description; the message is shown to the user."""


def parse_pace(value: Any) -> float:
    """'4:05', '4:05 /km' or 245 -> seconds per kilometre."""
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip().lower().replace("/km", "").replace("min", "").strip()
        if ":" in text:
            minutes, _, secs = text.partition(":")
            try:
                seconds = int(minutes) * 60 + float(secs)
            except ValueError as exc:
                raise WorkoutError(f"Could not read {value!r} as a pace.") from exc
        else:
            try:
                seconds = float(text) * 60
            except ValueError as exc:
                raise WorkoutError(
                    f"Could not read {value!r} as a pace. Use 'M:SS' per km."
                ) from exc
    if not 90 <= seconds <= 1800:
        raise WorkoutError(
            f"Pace {value!r} works out as {seconds:.0f} s/km, which is outside the "
            "plausible 1:30-30:00 range."
        )
    return seconds


def format_pace(seconds_per_km: float) -> str:
    minutes, secs = divmod(int(round(seconds_per_km)), 60)
    return f"{minutes}:{secs:02d}/km"


# Garmin keeps targetValueOne/Two as fields on the step itself, NOT inside the
# targetType object. Nesting them there uploads cleanly and silently loses the
# numbers, so each target returns its type and its values separately.


def _pace_target(pace: Any) -> tuple[dict[str, Any], tuple[float, float], str]:
    """Garmin stores pace targets as a metres-per-second range."""
    if isinstance(pace, (list, tuple)):
        if len(pace) != 2:
            raise WorkoutError(
                "A pace range needs exactly two values, e.g. ['4:00','4:10']."
            )
        bounds = sorted(parse_pace(p) for p in pace)
    else:
        centre = parse_pace(pace)
        bounds = [centre - PACE_WINDOW_SECONDS, centre + PACE_WINDOW_SECONDS]

    speeds = sorted(1000.0 / b for b in bounds)
    target = {
        "workoutTargetTypeId": TARGET_PACE_ZONE,
        "workoutTargetTypeKey": "pace.zone",
        "displayOrder": 6,
    }
    described = f" @ {format_pace(bounds[0])}-{format_pace(bounds[1])}"
    return target, (speeds[0], speeds[1]), described


def _hr_target(hr: Any) -> tuple[dict[str, Any], tuple[float, float], str]:
    if not isinstance(hr, (list, tuple)) or len(hr) != 2:
        raise WorkoutError("A heart-rate target needs two values, e.g. [150, 165].")
    low, high = sorted(float(v) for v in hr)
    if not 60 <= low <= 230 or not 60 <= high <= 230:
        raise WorkoutError(f"Heart-rate target {hr!r} is outside 60-230 bpm.")
    target = {
        "workoutTargetTypeId": TARGET_HEART_RATE_ZONE,
        "workoutTargetTypeKey": "heart.rate.zone",
        "displayOrder": 4,
    }
    return target, (low, high), f" @ {low:.0f}-{high:.0f} bpm"


NO_TARGET = {
    "workoutTargetTypeId": TARGET_NONE,
    "workoutTargetTypeKey": "no.target",
    "displayOrder": 1,
}


def _end_condition(kind: str) -> dict[str, Any]:
    if kind == "distance":
        return {
            "conditionTypeId": gw.ConditionType.DISTANCE,
            "conditionTypeKey": "distance",
            "displayOrder": 3,
            "displayable": True,
        }
    return {
        "conditionTypeId": gw.ConditionType.TIME,
        "conditionTypeKey": "time",
        "displayOrder": 2,
        "displayable": True,
    }


class _Builder:
    """Walks the step list, assigning the sequential order ids Garmin expects."""

    def __init__(self, default_pace: float) -> None:
        self.order = 0
        self.default_pace = default_pace
        self.estimated_seconds = 0.0
        self.lines: list[str] = []

    def _next_order(self) -> int:
        self.order += 1
        return self.order

    def build(self, steps: Sequence[Mapping[str, Any]], depth: int = 0) -> list[Any]:
        if not steps:
            raise WorkoutError("A workout needs at least one step.")
        return [self._build_one(step, depth) for step in steps]

    def _build_one(self, step: Mapping[str, Any], depth: int) -> Any:
        if not isinstance(step, Mapping):
            raise WorkoutError(f"Each step must be an object, got {step!r}.")
        kind = str(step.get("type", "interval")).strip().lower()

        if kind == "repeat":
            return self._build_repeat(step, depth)
        if kind not in STEP_TYPES:
            raise WorkoutError(
                f"Unknown step type {kind!r}. Use one of: "
                f"{', '.join(sorted(STEP_TYPES))}, repeat."
            )
        return self._build_step(kind, step, depth)

    def _build_repeat(self, step: Mapping[str, Any], depth: int) -> Any:
        if depth >= 1:
            raise WorkoutError("Repeat groups cannot be nested inside other repeats.")
        try:
            times = int(step.get("times") or step.get("iterations") or 0)
        except (TypeError, ValueError) as exc:
            raise WorkoutError("A repeat needs a whole number of 'times'.") from exc
        if times < 2:
            raise WorkoutError("A repeat needs 'times' of 2 or more.")

        order = self._next_order()
        self.lines.append(f"{times} x")
        before = self.estimated_seconds
        children = self.build(step.get("steps") or [], depth + 1)
        # The children were counted once; charge for the remaining iterations.
        self.estimated_seconds += (self.estimated_seconds - before) * (times - 1)

        return gw.RepeatGroup(
            stepOrder=order,
            stepType={
                "stepTypeId": gw.StepType.REPEAT,
                "stepTypeKey": "repeat",
                "displayOrder": 6,
            },
            numberOfIterations=times,
            workoutSteps=children,
            endCondition={
                "conditionTypeId": gw.ConditionType.ITERATIONS,
                "conditionTypeKey": "iterations",
                "displayOrder": 7,
                "displayable": False,
            },
            endConditionValue=float(times),
        )

    def _build_step(self, kind: str, step: Mapping[str, Any], depth: int) -> Any:
        distance = step.get("distance_meters")
        duration = step.get("duration_seconds")
        if distance is None and duration is None:
            raise WorkoutError(
                f"Step {kind!r} needs either 'duration_seconds' or 'distance_meters'."
            )
        if distance is not None and duration is not None:
            raise WorkoutError(
                f"Step {kind!r} has both 'duration_seconds' and 'distance_meters'; "
                "Garmin steps end on one or the other."
            )

        target, described = NO_TARGET, ""
        values: tuple[float, float] | None = None
        pace_for_estimate = self.default_pace
        if step.get("pace") is not None and step.get("hr") is not None:
            raise WorkoutError("A step can target pace or heart rate, not both.")
        if step.get("pace") is not None:
            target, values, described = _pace_target(step["pace"])
            # values are speeds in m/s; convert back for the duration estimate.
            pace_for_estimate = (1000.0 / values[0] + 1000.0 / values[1]) / 2
        elif step.get("hr") is not None:
            target, values, described = _hr_target(step["hr"])

        if distance is not None:
            value = float(distance)
            if value <= 0:
                raise WorkoutError("'distance_meters' must be positive.")
            condition = "distance"
            self.estimated_seconds += value / 1000.0 * pace_for_estimate
            amount = f"{value / 1000:.2f} km".rstrip("0").rstrip(".")
        else:
            value = float(duration)
            if value <= 0:
                raise WorkoutError("'duration_seconds' must be positive.")
            condition = "time"
            self.estimated_seconds += value
            minutes, secs = divmod(int(value), 60)
            amount = f"{minutes}m {secs:02d}s" if secs else f"{minutes}m"

        indent = "  " * (depth + 1) if depth else "  "
        self.lines.append(f"{indent}{kind}: {amount}{described}")

        type_id, type_key, display = STEP_TYPES[kind]
        target_values = (
            {"targetValueOne": values[0], "targetValueTwo": values[1]}
            if values
            else {}
        )
        return gw.ExecutableStep(
            stepOrder=self._next_order(),
            stepType={
                "stepTypeId": type_id,
                "stepTypeKey": type_key,
                "displayOrder": display,
            },
            endCondition=_end_condition(condition),
            endConditionValue=value,
            targetType=target,
            **target_values,
        )


def build_workout(
    name: str,
    sport: str,
    steps: Sequence[Mapping[str, Any]],
    description: str | None = None,
) -> tuple[Any, str, int]:
    """Return (workout model, human-readable summary, estimated seconds)."""
    if not name or not str(name).strip():
        raise WorkoutError("The workout needs a name.")
    sport_key = str(sport or "running").strip().lower()
    if sport_key not in SPORTS:
        raise WorkoutError(
            f"Unknown sport {sport!r}. Use one of: {', '.join(sorted(SPORTS))}."
        )

    model_cls, default_pace = SPORTS[sport_key]
    builder = _Builder(default_pace)
    built = builder.build(list(steps))
    estimated = int(round(builder.estimated_seconds))

    workout = model_cls(
        workoutName=str(name).strip(),
        description=description,
        estimatedDurationInSecs=estimated,
        workoutSegments=[
            gw.WorkoutSegment(
                segmentOrder=1,
                sportType=model_cls.model_fields["sportType"].default_factory(),
                workoutSteps=built,
            )
        ],
    )

    hours, rem = divmod(estimated, 3600)
    mins, secs = divmod(rem, 60)
    total = f"{hours}h {mins:02d}m" if hours else f"{mins}m {secs:02d}s"
    summary = "\n".join([f"{name} ({sport_key}, about {total})", *builder.lines])
    return workout, summary, estimated
