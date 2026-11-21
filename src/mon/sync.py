"""Synchronize verified official values and documents."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
import os
import sys
from time import monotonic

from .checkpoint import days_to_process, parse_checkpoint, serialize_checkpoint
from .config import NOTICE_FIELDS, OPTIONAL_OFFICIAL_FIELDS
from .documents import attach, download, pending_documents, read_notice
from .normalizer import normalize
from .odoo import TASK, amount_value, float_step, relation_id, selection_value
from .relevance import fold, is_relevant

MIN_PROPOSAL_WINDOW = timedelta(days=7)


def changed_values(old: dict, official: dict) -> dict:
    # Odoo can return integers for numeric fields, but False is an unset value.
    return {
        name: value for name, value in official.items()
        if old.get(name) != value or old.get(name) is False
    }


def official_task_name(record: dict) -> str | None:
    municipality, _, state = (record.get('municipality_state') or '').rpartition(' / ')
    process, organization = record.get('process_number'), record.get('organization')
    if process and organization and municipality and state:
        return f'{process} - {organization} - {municipality}/{state}'
    return None


class Synchronizer:
    def __init__(self, cfg, filters, pncp, odoo=None, *, apply=False):
        self.cfg = cfg
        self.filters = filters
        self.pncp = pncp
        self.odoo = odoo
        self.apply = apply
        self.stats = Counter()

    def _has_minimum_proposal_window(self, record: dict) -> bool:
        deadline = record.get('deadline')
        if deadline is None:
            self.stats['skipped_missing_deadline'] += 1
            return False
        remaining = datetime.fromisoformat(deadline).replace(tzinfo=UTC) - self.cfg.now()
        if remaining <= timedelta(0):
            self.stats['skipped_expired'] += 1
            return False
        if remaining < MIN_PROPOSAL_WINDOW:
            self.stats['skipped_short_deadline'] += 1
            return False
        return True

    def _values(self, record: dict, task: dict | None = None) -> dict:
        values = {}
        for source, (destination, meta) in self.odoo.mappings.items():
            if source in OPTIONAL_OFFICIAL_FIELDS and task and task.get(destination):
                continue
            value = record.get(source)
            if source == 'judgment_criterion':
                value = self.pncp.judgment_criterion(record['pncp_id'])
            if value is None:
                if source in OPTIONAL_OFFICIAL_FIELDS:
                    status = 'unverified' if not self.apply and source in NOTICE_FIELDS else 'unavailable'
                    self.stats[f'{status}_{source}'] += 1
                continue
            if source == 'estimated_value':
                if meta['type'] == 'monetary':
                    field = meta['currency_field']
                    currency = (
                        self.odoo.task_currency_default() if task is None
                        else task.get(field)
                    )
                    if currency is None or currency is False:
                        currency = self.odoo.assignable_task_currency(task)
                        values[field] = currency
                    step = self.odoo.currency_step(currency)
                else:
                    step = float_step(meta)
                value = amount_value(value, step)
            if meta['type'] == 'selection':
                value = selection_value(meta, value, required=source not in OPTIONAL_OFFICIAL_FIELDS)
                if value is None:
                    self.stats[f'unavailable_{source}'] += 1
                    continue
            values[destination] = value
        return values

    def _confirmed_values(self, row, values):
        amount = self.odoo.mappings.get('estimated_value')
        if amount and amount[1]['type'] == 'monetary':
            field = amount[1]['currency_field']
            if field in values:
                row = {**row, field: relation_id(row.get(field))}
        return not changed_values(row, values)

    def _create_task(self, identifier, official):
        new = {
            'project_id': self.cfg.project_id,
            'stage_id': self.cfg.stage_id,
            self.cfg.pncp_field: identifier,
            **official,
        }
        try:
            task_id = self.odoo.create_task(new)
        except RuntimeError as exc:
            row = self.odoo.tasks([identifier]).get(identifier)
            if row is None or not self._confirmed_values(row, official):
                raise RuntimeError('Unconfirmed task creation; checkpoint withheld') from exc
        else:
            row = self.odoo.tasks([identifier]).get(identifier)
            if row is None or row['id'] != task_id or not self._confirmed_values(row, official):
                raise RuntimeError('Created task association or official values were not confirmed')
        if relation_id(row.get('stage_id')) != self.cfg.stage_id:
            raise RuntimeError('Created task initial stage was not confirmed')
        amount = self.odoo.mappings.get('estimated_value')
        if (amount and amount[0] in official and amount[1]['type'] == 'monetary'
                and amount[1]['currency_field'] not in official):
            actual = row.get(amount[1]['currency_field'])
            if relation_id(actual) != self.odoo.task_currency_default():
                raise RuntimeError('Created task currency was not confirmed')
        return row

    def _update_task(self, identifier, old, changes):
        amount = self.odoo.mappings.get('estimated_value')
        if amount and amount[1]['type'] == 'monetary' and amount[1]['currency_field'] in changes:
            field = amount[1]['currency_field']
            current = self.odoo.tasks([identifier]).get(identifier)
            if current is None or current['id'] != old['id'] or current.get(field) is not False:
                raise RuntimeError('Task currency changed before synchronization')
            if self.odoo.assignable_task_currency(current) != changes[field]:
                raise RuntimeError('Task currency could not be reconfirmed')
        failure = None
        try:
            self.odoo.update_task(old['id'], changes)
        except RuntimeError as exc:
            failure = exc
        row = self.odoo.tasks([identifier]).get(identifier)
        if row is None or row['id'] != old['id'] or not self._confirmed_values(row, changes):
            raise RuntimeError('Unconfirmed task update; checkpoint withheld') from failure

    def _sync_batch(self, records: dict[str, dict], progress) -> None:
        if not records:
            return
        present = self.odoo.tasks(list(records)) if self.odoo else {}
        for identifier, record in records.items():
            progress()
            documents = self.pncp.documents(identifier)
            if self.odoo is None:
                self.stats['updates_unverified'] += 1
                self.stats['documents_unverified'] += len(documents)
                continue
            old = present.get(identifier)
            if self.apply:
                current = self.odoo.tasks([identifier]).get(identifier)
                if old and (current is None or current['id'] != old['id']):
                    raise RuntimeError('Task identity changed before synchronization')
                old = current
            if record.get('estimate_needs_confirmation') and not self.pncp.budget_is_public(identifier):
                record = {**record, 'estimated_value': None}
            name = official_task_name(record)
            if old is None and name is None:
                raise ValueError('Missing official process, organization or location for the task name')
            notice, notice_content = None, None
            if self.apply and any(
                field in NOTICE_FIELDS and (old is None or not old.get(destination))
                for field, (destination, _) in self.odoo.mappings.items()
            ):
                notices = [doc for doc in documents if fold(doc.kind) in {'edital', 'aviso de contratacao direta'}]
                if len(notices) == 1:
                    notice = notices[0]
                    notice_content = download(self.pncp.session, notice)
                    existing = self.odoo.attachments(old['id']) if old else ()
                    record = {**record, **read_notice(notice_content, notice, existing)}
            official = self._values(record, old)
            legacy_title = record.get('title')
            if old is None or (
                name and legacy_title and old.get('name') == f'PNCP {identifier} - {legacy_title[:130]}'
            ):
                official['name'] = name
            changes = changed_values(old, official) if old else {}
            if not self.apply:
                self.stats['requires_create'] += old is None
                self.stats['requires_update'] += bool(changes)
                existing = self.odoo.attachments(old['id']) if old and documents else []
                pending, unverified = pending_documents(documents, existing)
                self.stats['documents_pending'] += pending
                self.stats['documents_unverified'] += unverified
                continue

            if not self._has_minimum_proposal_window(record):
                self.stats['matched'] -= 1
                continue
            if old is None:
                created = self._create_task(identifier, official)
                self.stats['created'] += 1
                task_id = created['id']
            else:
                task_id = old['id']
                if changes:
                    self._update_task(identifier, old, changes)
                    self.stats['updated'] += 1
                else:
                    self.stats['unchanged'] += 1
            if documents:
                self.odoo.require_access(TASK, 'write', [task_id])
                existing = self.odoo.attachments(task_id)
                for document in documents:
                    progress()
                    if attach(
                        self.odoo, self.pncp.session, identifier, task_id, document, existing,
                        content=notice_content if document is notice else None,
                    ):
                        self.stats['documents_uploaded'] += 1
                    else:
                        self.stats['documents_present'] += 1

    def run(self):
        started = self.next_progress = monotonic()
        last_done = None
        if self.apply and self.odoo is None:
            raise ValueError('Odoo client is required for apply mode')
        if self.odoo:
            project = self.odoo.check_fields(write=self.apply)
            if (
                self.apply and os.getenv('ODOO_ALLOW_UNLINKED_TASKS') != 'true'
                and self.odoo.has_unlinked_tasks()
            ):
                raise ValueError(
                    'Existing tasks lack identifiers. Review duplicates before enabling '
                    'ODOO_ALLOW_UNLINKED_TASKS.'
                )
            if self.cfg.checkpoint_field:
                last_done = parse_checkpoint(project[self.cfg.checkpoint_field])
            if last_done and last_done > self.cfg.today:
                raise ValueError('Stored checkpoint exceeds the current date')

        if not self.apply:
            self.stats.update({
                'matched': 0, 'documents_pending': 0, 'documents_unverified': 0,
                **({'requires_create': 0, 'requires_update': 0} if self.odoo
                   else {'updates_unverified': 0}),
            })
        last_complete = last_done
        for day in days_to_process(
            self.cfg.initial_date, last_done, self.cfg.today,
            self.cfg.overlap_days, self.cfg.max_days,
        ):
            updated_identifiers = set()
            # Apply the preferred update snapshot first so replay cannot overwrite it with publication data.
            for kind in ('atualizacao', 'publicacao'):
                for modality in self.filters.modalities:
                    server_uf = self.filters.ufs[0] if len(self.filters.ufs) == 1 else None
                    completed = 0

                    def progress(force=False):
                        now = monotonic()
                        if force or now >= self.next_progress:
                            print(
                                f'Progress day={day} query={kind} modality={modality} '
                                f'pages_completed={completed} received={self.stats["seen"]} '
                                f'relevant={self.stats["matched"]} '
                                f'skipped_expired={self.stats["skipped_expired"]} '
                                f'skipped_short_deadline={self.stats["skipped_short_deadline"]} '
                                f'skipped_missing_deadline={self.stats["skipped_missing_deadline"]} '
                                f'tasks_created={self.stats["created"]} tasks_updated={self.stats["updated"]} '
                                f'attachments_uploaded={self.stats["documents_uploaded"]} '
                                f'attachments_present={self.stats["documents_present"]} '
                                f'elapsed_seconds={now - started:.0f} last_complete_day={last_complete}',
                                file=sys.stderr, flush=True,
                            )
                            self.next_progress = now + 30

                    progress(force=True)
                    for page in self.pncp.list_day_pages(
                        kind, day, modality, self.cfg.page_size, self.cfg.max_pages,
                        uf=server_uf,
                    ):
                        records = {}
                        self.stats['seen'] += len(page)
                        for item in page:
                            if not is_relevant(item, self.filters):
                                self.stats['filtered'] += 1
                                continue
                            record = normalize(item)
                            identifier = record['pncp_id']
                            if kind == 'atualizacao':
                                updated_identifiers.add(identifier)
                            elif identifier in updated_identifiers:
                                continue
                            if not self._has_minimum_proposal_window(record):
                                continue
                            self.stats['matched'] += 1
                            records[identifier] = record
                        self._sync_batch(records, progress)
                        completed += 1
                        progress(force=True)
            if self.apply and day < self.cfg.today and (last_done is None or day > last_done):
                self.odoo.write_checkpoint(serialize_checkpoint(day))
                last_done = day
            if day < self.cfg.today and (last_complete is None or day > last_complete):
                last_complete = day
            self.stats['days'] += 1
            print(f'Batch {day.isoformat()}: completed, mode={"apply" if self.apply else "preview"}, '
                  f'last_complete_day={last_complete}', flush=True)
        return dict(self.stats)
