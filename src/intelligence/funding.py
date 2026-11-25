"""Read-only funding observations and change detection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
CATALOGUE, OFFICIAL_PAGE, REGULATION, APPLICABLE_REVISION = range(4)
CRITICAL = {
    'deadline', 'applications_start', 'pre_proposal_deadline', 'full_proposal_deadline',
    'appeal_deadline', 'qualification_appeal_deadline', 'merit_appeal_deadline', 'restricted_stage_deadline',
    'continuous', 'official_status',
    'global_amount', 'max_per_proposal', 'min_per_proposal', 'currency', 'target', 'geography',
    'company_size', 'legal_requirements', 'trl', 'partner_requirement', 'counterpart',
    'eligible_expenses', 'areas',
}


@dataclass(frozen=True)
class Evidence:
    value: str
    url: str
    section: str
    page: int | None = None
    authority: int = OFFICIAL_PAGE
    published_at: str | None = None


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
    last_attempt_at: str = ''
    verification: str = 'unverified'
    calendar_status: str = 'unverified'
    conflicts: dict[str, list[Evidence]] = field(default_factory=dict)

    def record(
        self, key: str, value: str | None, url: str, section: str, page: int | None = None, *,
        authority: int = OFFICIAL_PAGE, published_at: str | None = None,
    ) -> None:
        if value and value.strip():
            self.add_evidence(key, Evidence(value.strip()[:2000], url, section, page, authority, published_at))

    def add_evidence(self, key: str, evidence: Evidence) -> None:
        existing = self.facts.get(key)
        if existing is None and key not in self.conflicts:
            self.facts[key] = evidence
            return
        candidates = list(self.conflicts.get(key, ()))
        candidates.extend(value for value in (existing, evidence) if value and value not in candidates)
        candidates.sort(key=lambda value: (-value.authority, value.value, value.url, value.section,
                                           value.page or 0, value.published_at or ''))
        best = candidates[0]
        if (key in CRITICAL or key.startswith('document:')) and not _agree(candidates):
            self.conflicts[key] = candidates
        else:
            self.conflicts.pop(key, None)
        highest = [value for value in candidates if value.authority == best.authority]
        if key in self.conflicts and not _agree(highest):
            self.facts.pop(key, None)
        else:
            precise = [value for value in highest if value.published_at]
            self.facts[key] = precise[0] if _agree(highest) and precise else best


def _agree(evidence: list[Evidence]) -> bool:
    values = {item.value.casefold().strip(' .') for item in evidence}
    timestamps = [datetime.fromisoformat(item.published_at) for item in evidence if item.published_at]
    aware = {stamp for stamp in timestamps if stamp.tzinfo is not None}
    local_times = {stamp.time() for stamp in timestamps if stamp.tzinfo is None}
    if not aware or len(aware) == 1 and next(iter(aware)).time() in local_times:
        return len(values) == 1 and len(local_times) <= 1
    return len(values) == 1 and len(aware) <= 1 and not local_times


def dated_status(
    today: date, start: date | None, end: date | None, *, now: datetime | None = None,
    start_at: datetime | None = None, end_at: datetime | None = None,
) -> str:
    if start and end and start > end:
        return 'unverified'
    if start_at and end_at and start_at > end_at:
        return 'unverified'
    if end_at and now:
        if now > end_at:
            return 'deadline_elapsed'
    elif end and end < today:
        return 'deadline_elapsed'
    if now and start_at and now < start_at:
        return 'future'
    if start and start > today:
        return 'future'
    if end and (not start or start <= today):
        return 'within_submission_window'
    return 'unverified'


def reconcile(
    previous: list[Opportunity], observed: list[Opportunity], attempted_at: str,
) -> tuple[list[Opportunity], list[dict[str, Any]]]:
    """Carry forward verified values when a read cannot confirm a replacement."""
    before = {item.identity: item for item in previous}
    current = {item.identity: item for item in observed}
    if len(before) != len(previous) or len(current) != len(observed):
        raise ValueError('Duplicate funding opportunity identity')
    retained = []
    for item in observed:
        old = before.get(item.identity)
        merged = replace(item, facts=dict(item.facts), documents=dict(item.documents),
                         conflicts={key: list(values) for key, values in item.conflicts.items()},
                         last_attempt_at=attempted_at)
        if old:
            merged.checked_at = old.checked_at
            for key, value in old.facts.items():
                new = item.facts.get(key)
                if (new is None or new.authority < value.authority
                        or (value.published_at and new.published_at is None and _agree([new, value]))):
                    if new is not None and not _agree([new, value]):
                        merged.conflicts[key] = list(dict.fromkeys([value, *merged.conflicts.get(key, []), new]))
                    if key in merged.conflicts:
                        merged.conflicts[key] = list(dict.fromkeys([value, *merged.conflicts[key]]))
                    merged.facts[key] = value
            for key, values in old.conflicts.items():
                if key not in item.facts and key not in item.conflicts:
                    merged.conflicts[key] = list(values)
            merged.documents = {**old.documents, **{url: digest for url, digest in item.documents.items()
                                                     if digest or url not in old.documents}}
            if item.modality == 'unverified':
                merged.modality = old.modality
            if item.status == 'unverified' or ('official_status' in old.facts
                                              and merged.facts.get('official_status') == old.facts['official_status']
                                              and old.status in {'closed', 'cancelled', 'suspended'}):
                merged.status = old.status
            if item.calendar_status == 'unverified':
                merged.calendar_status = old.calendar_status
        unresolved = any(not values or not _agree([entry for entry in values
                                                  if entry.authority == max(e.authority for e in values)])
                         for values in merged.conflicts.values())
        missing = bool(old and (old.facts.keys() - item.facts.keys()
                                or any(digest and not item.documents.get(url)
                                       for url, digest in old.documents.items())))
        merged.verification = 'partial' if item.verification == 'partial' or unresolved or missing else 'verified'
        if merged.verification == 'verified':
            merged.checked_at = attempted_at
        retained.append(merged)
    retained.extend(replace(old, last_attempt_at=attempted_at, verification='not_reverified')
                    for identity, old in before.items() if identity not in current)
    changes = compare(previous, retained)
    changes.extend({'identity': identity, 'kind': 'not_reverified'} for identity in sorted(before.keys() - current.keys()))
    return sorted(retained, key=lambda item: item.identity), changes


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
            if not _agree([before.facts[key], item.facts[key]]) or (
                before.facts[key].published_at != item.facts[key].published_at
            ):
                diffs[key] = {'before': asdict(before.facts[key]), 'after': asdict(item.facts[key])}
        for key in sorted(item.facts.keys() - before.facts.keys()):
            diffs[key] = {'after': asdict(item.facts[key])}
        if item.status != before.status and item.status not in {'unverified', 'deadline_elapsed', 'future',
                                                                'within_submission_window'}:
            diffs['status'] = {'before': before.status, 'after': item.status}
        for key in ('title', 'modality'):
            if getattr(before, key) != getattr(item, key) and getattr(item, key) != 'unverified':
                diffs[key] = {'before': getattr(before, key), 'after': getattr(item, key)}
        found = {url: digest for url, digest in item.documents.items()
                 if digest and before.documents.get(url) != digest}
        new_links = item.documents.keys() - before.documents.keys()
        if found:
            diffs['documents'] = {'verified': found, 'new_links': sorted(new_links & found.keys())}
        if diffs:
            changes.append({'identity': identity, 'kind': 'changed', 'fields': list(diffs), 'values': diffs})
        if new_links - found.keys():
            changes.append({'identity': identity, 'kind': 'document_links_observed',
                            'urls': sorted(new_links - found.keys())})
        if item.conflicts != before.conflicts:
            changes.append({'identity': identity, 'kind': 'evidence_review',
                            'conflicts': {key: [asdict(value) for value in values]
                                          for key, values in item.conflicts.items()}})
    changes.extend({'identity': key, 'kind': 'not_reverified'} for key in sorted(old.keys() - new.keys()))
    return changes


def opportunity_from_dict(row: dict[str, Any]) -> Opportunity:
    fields: dict[str, Any] = {
        **row, 'facts': {key: Evidence(**value) for key, value in row['facts'].items()},
        'conflicts': {key: [Evidence(**value) for value in values] for key, values in row.get('conflicts', {}).items()},
    }
    item = Opportunity(**fields)
    if (not isinstance(item.identity, str) or not item.identity or not isinstance(item.documents, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in item.documents.items())):
        raise ValueError('Invalid funding opportunity')
    return item


def load_baseline(path: Path) -> list[Opportunity]:
    with path.open('rb') as handle:
        body = handle.read(MAX_SNAPSHOT_BYTES + 1)
    if len(body) > MAX_SNAPSHOT_BYTES:
        raise ValueError('Funding snapshot exceeds size limit')
    try:
        data = json.loads(body)
        if data['mode'] != 'funding-preview' or not isinstance(data['opportunities'], list):
            raise ValueError
        return [opportunity_from_dict(row) for row in data['opportunities']]
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        raise ValueError('Invalid funding preview snapshot') from exc
