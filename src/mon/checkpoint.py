"""Checkpoint persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError('Missing checkpoint date')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('Invalid checkpoint date') from exc


@dataclass(frozen=True)
class Checkpoint:
    last_complete_day: date | None = None
    scanned_through: date | None = None
    pending: tuple[date, ...] = ()

    @classmethod
    def load(cls, value: object) -> Checkpoint:
        if not value:
            return cls()
        if not isinstance(value, str):
            raise ValueError('Invalid checkpoint')
        data = json.loads(value)
        if not isinstance(data, dict) or type(data.get('version')) is not int:
            raise ValueError('Unsupported checkpoint version')
        if data['version'] == 1:
            day = _date(data.get('last_complete_day'))
            return cls(day, day)
        if data['version'] != 2:
            raise ValueError('Unsupported checkpoint version')
        pending_days = data.get('pending_days')
        if 'last_complete_day' not in data or not isinstance(pending_days, list) or not pending_days:
            raise ValueError('Invalid pending checkpoint')
        last = data['last_complete_day']
        complete = None if last is None else _date(last)
        scanned = _date(data.get('scanned_through'))
        pending = tuple(_date(day) for day in pending_days)
        if (complete and complete > scanned) or max(pending) > scanned or len(pending) != len(set(pending)):
            raise ValueError('Invalid pending checkpoint dates')
        return cls(complete, scanned, pending)

    def serialize(self) -> str:
        if self.scanned_through is None:
            raise ValueError('Missing scanned checkpoint date')
        if not self.pending:
            return serialize_checkpoint(self.scanned_through)
        return json.dumps({
            'version': 2,
            'last_complete_day': self.last_complete_day.isoformat() if self.last_complete_day else None,
            'scanned_through': self.scanned_through.isoformat(),
            'pending_days': [day.isoformat() for day in self.pending],
        }, separators=(',', ':'))

    def record(self, day: date, *, deferred: bool = False) -> Checkpoint:
        scanned = max(day, self.scanned_through or day)
        pending = tuple(d for d in self.pending if d != day)
        if deferred:
            pending += (day,)
        return Checkpoint(self.last_complete_day if pending else scanned, scanned, pending)

    def planned_days(self, initial: date | None, today: date, overlap_days: int, max_days: int):
        if self.scanned_through and self.scanned_through > today:
            raise ValueError('Stored checkpoint exceeds the current date')
        if not self.pending:
            yield from days_to_process(initial, self.scanned_through, today, overlap_days, max_days)
            return
        # Reserve retry capacity without letting a deferred day consume the forward slot.
        scheduled = [day for day in days_to_process(
            initial, self.scanned_through, today, overlap_days, max(1, max_days - 1),
        ) if day not in self.pending and (max_days > 1 or day < today)]
        yield from sorted([*scheduled, *self.pending[:max_days - len(scheduled)]])


def parse_checkpoint(value: object) -> date | None:
    return Checkpoint.load(value).last_complete_day


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
