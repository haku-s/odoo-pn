"""Read and append versioned funding observations through the Odoo funding contract."""

from dataclasses import asdict
from typing import Any

from .funding import Opportunity, opportunity_from_dict
from .odoo import FUNDING, FUNDING_REVISION, OdooClient, positive_id

CONTRACT = 'funding-monitor-v1'
PAGE_LIMIT = 16
MAX_ROWS = 10000
RESPONSE_LIMIT = 8 * 1024 * 1024


class FundingStore:
    def __init__(self, client: OdooClient):
        self.client = client
        self.revisions: dict[str, int] = {}

    def check(self, *, write: bool = False) -> None:
        for model, fields in (
            (FUNDING, {'x_name': ('char', None), 'x_identity': ('char', None),
                       'x_current_id': ('many2one', FUNDING_REVISION), 'x_generation': ('integer', None),
                       'x_checked_at': ('datetime', None), 'x_last_attempt_at': ('datetime', None),
                       'x_verification': ('char', None)}),
            (FUNDING_REVISION, {'x_opportunity_id': ('many2one', FUNDING),
                               'x_revision': ('integer', None), 'x_state': ('json', None)}),
        ):
            metadata = self.client.fields(model)
            for key, (kind, relation) in fields.items():
                field = metadata.get(key, {})
                if field.get('type') != kind or (relation and field.get('relation') != relation):
                    raise ValueError(f'Unexpected funding field: {model}.{key}')
        if self.client.call(FUNDING, 'funding_contract', for_write=write) != {
            'contract': CONTRACT, 'write_checked': write,
        }:
            raise ValueError('Unsupported Odoo funding contract')

    @staticmethod
    def _read(row: Any) -> tuple[int, int, Opportunity]:
        if (not isinstance(row, dict) or not positive_id(row.get('id')) or not positive_id(row.get('revision'))
                or not isinstance(row.get('state'), dict)):
            raise ValueError('Invalid stored funding revision')
        item = opportunity_from_dict(row['state'])
        if item.identity != row.get('identity'):
            raise ValueError('Stored funding identity does not match its revision')
        return row['id'], row['revision'], item

    def load(self) -> list[Opportunity]:
        items = []
        revisions: dict[str, int] = {}
        after_id = 0
        while True:
            page = self.client.call(FUNDING, 'funding_page', after_id=after_id, limit=PAGE_LIMIT,
                                    response_limit=RESPONSE_LIMIT)
            if not isinstance(page, list) or len(page) > PAGE_LIMIT:
                raise ValueError('Invalid Odoo funding page')
            for row in page:
                record_id, revision, item = self._read(row)
                if record_id <= after_id or item.identity in revisions:
                    raise ValueError('Invalid Odoo funding pagination or duplicate identity')
                items.append(item)
                revisions[item.identity] = revision
                after_id = record_id
            if len(items) > MAX_ROWS:
                raise ValueError('Odoo funding history exceeds record limit')
            if len(page) < PAGE_LIMIT:
                self.revisions = revisions
                return items

    def append(self, item: Opportunity) -> None:
        desired = asdict(item)
        expected = self.revisions.get(item.identity, 0)
        try:
            self.client.call(FUNDING, 'funding_append', identity=item.identity,
                             expected_revision=expected, state=desired)
        except RuntimeError:
            # The connection can fail after the server commits. Only a read can confirm it.
            pass
        confirmed = self.client.call(FUNDING, 'funding_head', identity=item.identity, response_limit=RESPONSE_LIMIT)
        if confirmed is None:
            raise RuntimeError('Odoo funding revision could not be confirmed')
        _, revision, actual = self._read(confirmed)
        if revision not in (expected, expected + 1) or actual != item:
            raise RuntimeError('Odoo funding revision changed concurrently or could not be confirmed')
        self.revisions[item.identity] = revision
