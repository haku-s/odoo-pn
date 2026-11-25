"""Store official funding observations in standard Odoo models."""

import hashlib
import json
import re
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any, Literal

from .funding import Opportunity, opportunity_from_dict
from .odoo import FUNDING, FUNDING_REVISION, OdooClient, positive_id, relation_id

HEAD_FIELDS = {
    'x_name': 'char', 'x_identity': 'char', 'x_institution': 'char', 'x_modality': 'char',
    'x_official_url': 'char', 'x_official_status': 'char', 'x_submission_status': 'char', 'x_operational_status': 'char',
    'x_deadline': 'date', 'x_deadline_time': 'char', 'x_currency': 'char',
    'x_global_amount': 'float', 'x_min_amount': 'float', 'x_max_amount': 'float',
    'x_has_global_amount': 'boolean', 'x_has_min_amount': 'boolean', 'x_has_max_amount': 'boolean',
    'x_conflicts': 'boolean', 'x_compatibility': 'char', 'x_verification': 'char',
    'x_checked_at': 'datetime', 'x_last_attempt_at': 'datetime',
    'x_current_digest': 'char', 'x_revision_number': 'integer', 'x_revisions': 'one2many',
}
REVISION_FIELDS = {'x_name': 'char', 'x_opportunity_id': 'many2one', 'x_number': 'integer',
                   'x_digest': 'char', 'x_evidence': 'text'}
TEMPORAL = {'checked_at', 'last_attempt_at', 'verification', 'calendar_status', 'compatibility'}
PAGE_LIMIT = 40
MAX_ROWS = 10000


def _timestamp(value: str) -> str | bool:
    if not value:
        return False
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError('Funding verification timestamp must include a timezone')
    return instant.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _iso(value: str | Literal[False]) -> str:
    return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).isoformat() if value else ''


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def funding_values(item: Opportunity) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = json.dumps({key: value for key, value in asdict(item).items() if key not in TEMPORAL},
                          sort_keys=True, ensure_ascii=False, allow_nan=False)
    deadline = item.facts.get('pre_proposal_deadline') or item.facts.get('deadline')
    facts = {key: fact.value for key, fact in item.facts.items()}
    amounts = {}
    for official, field in (('global_amount', 'global'), ('min_per_proposal', 'min'), ('max_per_proposal', 'max')):
        raw = facts.get(official, '')
        number = re.fullmatch(r'(?:R\$|€)\s*(\d{1,3}(?:\.\d{3})+|\d+)(,\d{1,2})?'
                              r'(?:\s+(mil|milhão|milhões))?', raw, re.IGNORECASE)
        parsed = None
        if number:
            units, cents, magnitude = number.groups()
            value = float(units.replace('.', '') + (cents or '').replace(',', '.'))
            value *= {'mil': 1000, 'milhão': 1000000, 'milhões': 1000000}.get((magnitude or '').lower(), 1)
            if value <= 1e16:
                parsed = value
        amounts[f'x_{field}_amount'] = parsed if parsed is not None else 0.0
        amounts[f'x_has_{field}_amount'] = parsed is not None
    return {
        'x_name': item.title, 'x_identity': item.identity, 'x_institution': item.institution,
        'x_modality': item.modality, 'x_official_url': item.url, 'x_official_status': facts.get('official_status') or False,
        'x_operational_status': item.status,
        'x_submission_status': item.calendar_status, 'x_deadline': date.fromisoformat(deadline.value).isoformat()
        if deadline else False, 'x_deadline_time': deadline.published_at or False if deadline else False,
        'x_currency': facts.get('currency') or False, **amounts, 'x_conflicts': bool(item.conflicts),
        'x_compatibility': item.compatibility, 'x_verification': item.verification,
        'x_checked_at': _timestamp(item.checked_at), 'x_last_attempt_at': _timestamp(item.last_attempt_at),
    }, {'x_digest': _digest(evidence), 'x_evidence': evidence}


class FundingStore:
    def __init__(self, client: OdooClient):
        self.client = client
        self.heads: dict[str, tuple[int, int, str, str | bool]] = {}

    def check(self, *, write: bool = False) -> None:
        self.client.check_fields(FUNDING, HEAD_FIELDS, relations={'x_revisions': FUNDING_REVISION},
                                 write=write, access=('read', 'create', 'write') if write else ('read',))
        self.client.check_fields(FUNDING_REVISION, REVISION_FIELDS, relations={'x_opportunity_id': FUNDING},
                                 write=write, access=('read', 'create') if write else ('read',))
        if write:
            inverse = self.client.fields(FUNDING)['x_revisions'].get('relation_field')
            if inverse != 'x_opportunity_id':
                raise ValueError('Incompatible funding revision inverse field')

    @staticmethod
    def _token(row: dict[str, Any]) -> tuple[int, int, str, str | bool]:
        if (not positive_id(row.get('id')) or not positive_id(row.get('x_revision_number'))
                or not isinstance(row.get('x_current_digest'), str)):
            raise ValueError('Invalid Odoo funding head')
        return row['id'], row['x_revision_number'], row['x_current_digest'], row['x_last_attempt_at']

    def _revision(self, head: dict[str, Any]) -> Opportunity:
        record_id, number, digest, _ = self._token(head)
        revisions = self.client.storage_rows(
            FUNDING_REVISION, [['x_opportunity_id', '=', record_id], ['x_number', '=', number]],
            ['x_opportunity_id', 'x_number', 'x_digest', 'x_evidence'], 2,
        )
        if len(revisions) != 1:
            raise ValueError('Missing or duplicate Odoo funding revision')
        revision = revisions[0]
        content = revision.get('x_evidence')
        if (not isinstance(content, str) or _digest(content) != digest or revision.get('x_digest') != digest
                or relation_id(revision.get('x_opportunity_id')) != record_id or revision.get('x_number') != number):
            raise ValueError('Odoo funding evidence does not match its head')
        state = json.loads(content)
        if not isinstance(state, dict) or state.get('identity') != head.get('x_identity'):
            raise ValueError('Stored funding identity does not match its revision')
        return opportunity_from_dict(state | {
            'checked_at': _iso(head['x_checked_at']), 'last_attempt_at': _iso(head['x_last_attempt_at']),
            'verification': head['x_verification'], 'calendar_status': head['x_submission_status'],
            'compatibility': head['x_compatibility'],
        })

    def load(self) -> list[Opportunity]:
        items = []
        heads = {}
        after_id = 0
        while True:
            page = self.client.storage_rows(FUNDING, [['id', '>', after_id]],
                                            list(HEAD_FIELDS.keys() - {'x_revisions'}), PAGE_LIMIT)
            for row in page:
                token = self._token(row)
                item = self._revision(row)
                if token[0] <= after_id or item.identity in heads:
                    raise ValueError('Invalid Odoo funding pagination or duplicate identity')
                items.append(item)
                heads[item.identity] = token
                after_id = token[0]
            if len(items) > MAX_ROWS:
                raise ValueError('Odoo funding history exceeds record limit')
            if len(page) < PAGE_LIMIT:
                self.heads = heads
                return items

    def _head(self, identity: str) -> dict[str, Any]:
        rows = self.client.storage_rows(FUNDING, [['x_identity', '=', identity]],
                                        list(HEAD_FIELDS.keys() - {'x_revisions'}), 2)
        if len(rows) > 1:
            raise ValueError('Duplicate Odoo funding identity')
        return rows[0] if rows else {}

    def append(self, item: Opportunity) -> bool:
        desired, revision = funding_values(item)
        current = self._head(item.identity)
        token = self._token(current) if current else None
        if token != self.heads.get(item.identity):
            raise RuntimeError('Odoo funding head changed since collection')
        changes = {key: value for key, value in desired.items() if current.get(key) != value}
        new_revision = not token or token[2] != revision['x_digest']
        if new_revision:
            number = token[1] + 1 if token else 1
            changes.update(x_current_digest=revision['x_digest'], x_revision_number=number,
                           x_revisions=[[0, 0, revision | {'x_number': number, 'x_name': str(number)}]])
        elif not changes:
            return False
        self.client.storage_write(FUNDING, token[0] if token else None, changes)
        confirmed = self._head(item.identity)
        if (not confirmed or (token and confirmed['id'] != token[0])
                or any(confirmed.get(key) != value for key, value in changes.items() if key != 'x_revisions')):
            raise RuntimeError('Odoo funding write could not be confirmed')
        if new_revision:
            actual = self._revision(confirmed)
            if actual != item:
                raise RuntimeError('Odoo funding revision could not be confirmed')
        self.heads[item.identity] = self._token(confirmed)
        return True
