"""Read-only funding observations and change detection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import json
from pathlib import Path
from typing import Any

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Evidence:
    value: str
    url: str
    section: str
    page: int | None = None


@dataclass
class Opportunity:
    identity: str
    institution: str
    title: str
    modality: str
    url: str
    facts: dict[str, Evidence] = field(default_factory=dict)
    status: str = 'unverified'
    documents: dict[str, str] = field(default_factory=dict)
    compatibility: str = 'insufficient_information'
    issues: list[str] = field(default_factory=list)
    checked_at: str = ''

    def record(self, key: str, value: str | None, url: str, section: str, page: int | None = None) -> None:
        if value and value.strip():
            self.facts[key] = Evidence(value.strip()[:2000], url, section, page)


def dated_status(today: date, start: date | None, end: date | None) -> str:
    if end and end < today:
        return 'deadline_elapsed'
    if start and start > today:
        return 'future'
    if end and (not start or start <= today):
        return 'within_submission_window'
    return 'unverified'


def compare(previous: list[Opportunity], current: list[Opportunity]) -> list[dict[str, Any]]:
    old = {item.identity: item for item in previous}
    new = {item.identity: item for item in current}
    if len(old) != len(previous) or len(new) != len(current):
        raise ValueError('Duplicate funding opportunity identity')
    changes = []
    for identity, item in new.items():
        before = old.get(identity)
        if before is None:
            changes.append({'identity': identity, 'kind': 'new'})
            continue
        diffs: dict[str, Any] = {}
        for key in sorted(before.facts.keys() & item.facts.keys()):
            if before.facts[key].value != item.facts[key].value:
                diffs[key] = {'before': asdict(before.facts[key]), 'after': asdict(item.facts[key])}
        for key in sorted(item.facts.keys() - before.facts.keys()):
            diffs[key] = {'after': asdict(item.facts[key])}
        if item.status != before.status and item.status != 'unverified':
            diffs['status'] = {'before': before.status, 'after': item.status}
        for key in ('title', 'modality'):
            if getattr(before, key) != getattr(item, key) and getattr(item, key) != 'unverified':
                diffs[key] = {'before': getattr(before, key), 'after': getattr(item, key)}
        found = {url: digest for url, digest in item.documents.items()
                 if digest and before.documents.get(url) != digest}
        new_links = item.documents.keys() - before.documents.keys()
        if found or new_links:
            diffs['documents'] = {'verified': found, 'new_links': sorted(new_links)}
        if diffs:
            changes.append({'identity': identity, 'kind': 'changed', 'fields': list(diffs), 'values': diffs})
    changes.extend({'identity': key, 'kind': 'not_reverified'} for key in sorted(old.keys() - new.keys()))
    return changes


def load_baseline(path: Path) -> list[Opportunity]:
    with path.open('rb') as handle:
        body = handle.read(MAX_SNAPSHOT_BYTES + 1)
    if len(body) > MAX_SNAPSHOT_BYTES:
        raise ValueError('Funding snapshot exceeds size limit')
    try:
        data = json.loads(body)
        if data['mode'] != 'funding-preview' or not isinstance(data['opportunities'], list):
            raise ValueError
        items = []
        for row in data['opportunities']:
            fields: dict[str, Any] = {**row, 'facts': {key: Evidence(**value) for key, value in row['facts'].items()}}
            item = Opportunity(**fields)
            if (not isinstance(item.identity, str) or not item.identity or not isinstance(item.documents, dict)
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in item.documents.items())):
                raise ValueError
            items.append(item)
        return items
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        raise ValueError('Invalid funding preview snapshot') from exc
