#!/usr/bin/env python3
"""
vacation_lights.py — Simulate presence by randomly cycling indoor lights.

Usage:
    python3 vacation_lights.py
    python3 vacation_lights.py --list         # show discovered lights and exit
    python3 vacation_lights.py --dry-run      # print commands without running them
    python3 vacation_lights.py --interval 20  # change interval in minutes (default: 30)

Lights are discovered dynamically from Home Assistant at startup.
Groups are preferred over individual bulbs (a group controls all its members).
Individual bulbs are excluded when a group already covers the same room.

Runs until Ctrl+C. Only operates between ACTIVE_START and ACTIVE_END hours.
"""

import argparse
import json
import random
import subprocess
import sys
import time
from datetime import datetime

# Entity ID substrings to always exclude (outdoor, utility, non-light devices).
EXCLUDE_PATTERNS = [
    "porch",        # outdoor
    "shed",         # outdoor / utility
    "storage",      # utility
    "broom_closet",  # utility
    "portique",     # outdoor structure
    "apollo_air",   # air purifier RGB — not a room light
    "ezsp",         # duplicate Z-Wave controller entity
    "silicon_labs",  # same
]

# Lights turn off at this hour regardless of sunset (23 = 11 pm).
ACTIVE_END = 23  # 23:00

# How many lights to keep on at once (chosen randomly from this range).
MIN_ON = 1
MAX_ON = 4

# Brightness range for lights that are turned on.
MIN_BRIGHTNESS = 80
MAX_BRIGHTNESS = 220


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def hacli_json(args: list[str]) -> list[dict]:
    result = subprocess.run(["hacli"] + args, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def discover_lights() -> list[str]:
    """Query HA for all light entities and return a curated list.

    Strategy:
    - Fetch all states, keep light.* entities that are not unavailable.
    - Apply EXCLUDE_PATTERNS.
    - Prefer groups: if a _group entity covers a room, skip the individual
      bulbs in that room (identified by checking that all words from the
      group's name appear in the individual's entity_id).
    """
    states = hacli_json(["state", "list"])

    all_lights = [
        s["entity_id"]
        for s in states
        if s["entity_id"].startswith("light.")
        and s.get("state") != "unavailable"
    ]

    # Apply exclusion patterns.
    def excluded(entity_id: str) -> bool:
        return any(p in entity_id for p in EXCLUDE_PATTERNS)

    lights = [e for e in all_lights if not excluded(e)]

    # Split into groups and individuals.
    groups = [e for e in lights if e.endswith("_group")]
    individuals = [e for e in lights if not e.endswith("_group")]

    # For each group, extract its "room words" (everything between light. and _group).
    def room_words(group_id: str) -> list[str]:
        name = group_id.removeprefix("light.").removesuffix("_group")
        return name.split("_")

    # Exclude individuals whose entity_id contains ALL words of any group.
    def covered_by_group(entity_id: str) -> bool:
        return any(
            all(word in entity_id for word in room_words(g))
            for g in groups
        )

    remaining_individuals = [e for e in individuals if not covered_by_group(e)]

    return groups + remaining_individuals


def hacli(args: list[str], dry_run: bool) -> None:
    cmd = ["hacli"] + args
    if dry_run:
        print("  DRY-RUN:", " ".join(cmd))
        return
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")


def turn_off(entity: str, dry_run: bool) -> None:
    hacli(["service", "call", "light", "turn_off",
          "--field", f"entity_id={entity}"], dry_run)


def turn_on(entity: str, brightness: int, dry_run: bool) -> None:
    hacli(
        [
            "service", "call", "light", "turn_on",
            "--field", f"entity_id={entity}",
            "--field", f"brightness={brightness}",
        ],
        dry_run,
    )


def fetch_sunset() -> datetime:
    """Return today's sunset time as a local-timezone-aware datetime."""
    from datetime import timedelta
    raw = subprocess.run(
        ["hacli", "template", "{{ state_attr('sun.sun', 'next_setting') }}"],
        capture_output=True, text=True,
    ).stdout.strip()
    sunset = datetime.fromisoformat(raw).astimezone()
    # next_setting is always the *next* upcoming sunset — if it's tomorrow
    # (because today's sunset has already passed), roll back one day.
    now = datetime.now().astimezone()
    if sunset.date() > now.date():
        sunset -= timedelta(days=1)
    return sunset


def is_active(sunset: datetime) -> bool:
    now = datetime.now().astimezone()
    return sunset <= now and now.hour < ACTIVE_END


def cycle(current_on: set[str], lights: list[str], sunset: datetime, dry_run: bool) -> set[str]:
    if not is_active(sunset):
        if current_on:
            log("Outside active hours — turning all lights off.")
            for entity in current_on:
                turn_off(entity, dry_run)
        return set()

    count = random.randint(MIN_ON, min(MAX_ON, len(lights)))
    next_on = set(random.sample(lights, count))

    to_turn_off = current_on - next_on
    to_turn_on = next_on - current_on

    log(f"Cycling: {count} light(s) on — turning off {len(to_turn_off)}, turning on {len(to_turn_on)}")

    for entity in to_turn_off:
        log(f"  OFF  {entity}")
        turn_off(entity, dry_run)
        time.sleep(0.5)

    for entity in to_turn_on:
        brightness = random.randint(MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        log(f"  ON   {entity}  (brightness={brightness})")
        turn_on(entity, brightness, dry_run)
        time.sleep(0.5)

    return next_on


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate presence with random light cycling.")
    parser.add_argument("--list", action="store_true",
                        help="Show discovered lights and exit.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without executing them.")
    parser.add_argument("--now", action="store_true",
                        help="Start immediately without waiting for sunset.")
    parser.add_argument("--interval", type=float, default=30, metavar="MINUTES",
                        help="Minutes between light changes (default: 30).")
    args = parser.parse_args()

    log("Discovering lights from Home Assistant…")
    lights = discover_lights()

    if not lights:
        log("No eligible lights found. Check EXCLUDE_PATTERNS or HA connectivity.")
        sys.exit(1)

    log(f"Found {len(lights)} light(s):")
    for entity in lights:
        print(f"    {entity}")

    if args.list:
        return

    interval_seconds = args.interval * 60
    if args.dry_run:
        log("DRY-RUN mode — no commands will be sent.")

    sunset = fetch_sunset()
    if args.now:
        sunset = datetime.now().astimezone()
        log(
            f"--now: starting immediately (sunset is actually {fetch_sunset().strftime('%H:%M')}, active until {ACTIVE_END:02d}:00)")
    else:
        log(f"Sunset today: {sunset.strftime('%H:%M')} — lights active until {ACTIVE_END:02d}:00")
    log(f"Interval: {args.interval}m")

    current_on: set[str] = set()
    last_date = datetime.now().date()
    try:
        while True:
            # Refresh sunset once per day.
            today = datetime.now().date()
            if today != last_date:
                sunset = fetch_sunset()
                last_date = today
                log(f"New day — sunset updated to {sunset.strftime('%H:%M')}")

            current_on = cycle(current_on, lights, sunset, args.dry_run)
            next_change = datetime.fromtimestamp(
                time.time() + interval_seconds).strftime("%H:%M:%S")
            log(f"Next change at {next_change}. Press Ctrl+C to stop.")
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        log("Stopping — turning all lights off.")
        for entity in current_on:
            turn_off(entity, args.dry_run)
        log("Done.")


if __name__ == "__main__":
    main()
