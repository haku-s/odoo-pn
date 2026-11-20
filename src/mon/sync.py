"""Batch synchronization utilities."""

from __future__ import annotations

from collections import Counter
from datetime import date
import os

from .checkpoint import (
    days_to_process,
    parse_checkpoint,
    serialize_checkpoint,
)
from .normalizer import normalize
from .relevance import is_relevant


class Synchronizer:
    def __init__(
        self,
        cfg,
        filters,
        pncp,
        odoo=None,
        *,
        apply=False,
    ):
        self.cfg = cfg
        self.filters = filters
        self.pncp = pncp
        self.odoo = odoo
        self.apply = apply
        self.stats = Counter()

    
    def _values(self, record: dict) -> dict:
        return {
            dest: (
                False if record[source] is None else record[source]
            )
            for source, dest in self.cfg.field_map.items()
            if source in record
        }


    def _sync_batch(self, records: dict[str, dict]) -> None:
        if not records:
            return

        present = self.odoo.tasks(list(records))

        for pncp_id, record in records.items():
            values = self._values(record)
            old = present.get(pncp_id)

            if old is None:
                title = (
                    f"PNCP {pncp_id} - "
                    f"{str(record.get('objeto') or '')[:130]}"
                ).strip()

                new = {
                    'project_id': self.cfg.project_id,
                    'name': title,
                    self.cfg.pncp_field: pncp_id,
                    **values,
                }

                if self.cfg.stage_id:
                    new['stage_id'] = self.cfg.stage_id

                self.odoo.create_task(new)
                self.stats['created'] += 1

            else:
                changed = {}

                for dest, val in values.items():
                    old_value = old.get(dest)

                    if (
                        old_value is False
                        and val in ('', None, False)
                    ):
                        continue

                    if old_value != val:
                        changed[dest] = val

                if changed:
                    self.odoo.update_task(
                        old['id'],
                        changed,
                    )
                    self.stats['updated'] += 1
                else:
                    self.stats['unchanged'] += 1

    def run(self):
        last_done = None

        if self.apply:
            if self.odoo is None:
                raise ValueError(
                    'Remote client is required for write mode'
                )

            self.cfg.require_odoo(write=True)
            self.odoo.check_fields(write=True)

            if (
                os.getenv('ODOO_ALLOW_UNLINKED_TASKS') != 'true'
                and self.odoo.has_unlinked_tasks()
            ):
                raise ValueError(
                    'Existing records lack required identifiers. '
                    'Review duplicates before setting '
                    'ODOO_ALLOW_UNLINKED_TASKS=true'
                )

            project = self.odoo.project([
                self.cfg.checkpoint_field,
            ])

            last_done = parse_checkpoint(
                project.get(self.cfg.checkpoint_field)
            )

            if last_done and last_done > self.cfg.today:
                raise ValueError(
                    'Stored checkpoint exceeds the current date'
                )

        for day in days_to_process(
            self.cfg.initial_date,
            last_done,
            self.cfg.today,
            self.cfg.overlap_days,
            self.cfg.max_days,
        ):
            buffer: dict[str, dict] = {}

            for kind in ('publicacao', 'atualizacao'):
                for modality in self.filters.modalities:
                    server_uf = (
                        self.filters.ufs[0]
                        if len(self.filters.ufs) == 1
                        else None
                    )

                    for item in self.pncp.list_day(
                        kind,
                        day,
                        modality,
                        self.cfg.page_size,
                        self.cfg.max_pages,
                        uf=server_uf,
                    ):
                        self.stats['seen'] += 1

                        if not is_relevant(item, self.filters):
                            self.stats['filtered'] += 1
                            continue

                        rec = normalize(item)
                        self.stats['matched'] += 1

                        buffer[rec['pncp_id']] = rec

                        if len(buffer) >= 50:
                            if self.apply:
                                self._sync_batch(buffer)

                            buffer.clear()

            if self.apply:
                self._sync_batch(buffer)

                if day < self.cfg.today:
                    self.odoo.write_checkpoint(
                        serialize_checkpoint(day)
                    )

            self.stats['days'] += 1

            print(
                f'Batch {day.isoformat()}: completed, '
                f'mode={"apply" if self.apply else "preview"}'
            )

        return dict(self.stats)
