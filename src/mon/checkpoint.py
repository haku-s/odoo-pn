"""Checkpoint persist."""
from __future__ import annotations
import json
from datetime import date, timedelta


def parse_checkpoint(value: object) -> date | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError('Checkpoint')
    data = json.loads(value)
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('version not support')
    day = date.fromisoformat(data['last_complete_day'])
    return day


def serialize_checkpoint(day: date) -> str:
    return json.dumps({'version': 1, 'last_complete_day': day.isoformat()}, separators=(',', ':'))



def days_to_process(initial: date, last_done: date | None, today: date,
                    overlap_days: int, max_days: int):
    # Replay the overlap without discarding unfinished days.
    start = (
        initial
        if last_done is None
        else last_done - timedelta(days=overlap_days - 1)
    )
    if start > today:
        raise ValueError('Checkpoint future')
    day = start
    count = 0
    while day <= today and count < max_days:
        yield day
        day += timedelta(days=1)
        count += 1

