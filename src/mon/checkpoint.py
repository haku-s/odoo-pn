"""Checkpoint persistence."""

from __future__ import annotations

import json
from datetime import date, timedelta


def parse_checkpoint(value: object) -> date | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError('Invalid checkpoint')
    data = json.loads(value)
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('Unsupported checkpoint version')
    day = data.get('last_complete_day')
    if not isinstance(day, str):
        raise ValueError('Missing checkpoint date')
    try:
        return date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError('Invalid checkpoint date') from exc


def serialize_checkpoint(day: date) -> str:
    return json.dumps(
        {'version': 1, 'last_complete_day': day.isoformat()},
        separators=(',', ':'),
    )


def days_to_process(
    initial: date | None,
    last_done: date | None,
    today: date,
    overlap_days: int,
    max_days: int,
):
    # Leave one slot for a new day even when the configured overlap fills the run.
    start = (
        initial if last_done is None
        else last_done - timedelta(days=min(overlap_days, max_days - 1) - 1)
    )
    if start is None:
        raise ValueError('Set PNCP_INITIAL_DATE to the earliest unprocessed day when no checkpoint exists')
    if (last_done or start) > today:
        raise ValueError('Checkpoint future')
    day = start
    count = 0
    while day <= today and count < max_days:
        yield day
        day += timedelta(days=1)
        count += 1
