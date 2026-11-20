
"""Remote service client."""

from __future__ import annotations

import requests

from .config import Settings, ALLOWED_FIELDS


FIELD_TYPES = {
    'pncp': {'char', 'text'},
    'checkpoint': {'text'},
    'objeto': {'char', 'text'},
    'numero_edital': {'char', 'text'},
    'orgao': {'char', 'text'},
    'cnpj': {'char', 'text'},
    'uf': {'char', 'text'},
    'municipio': {'char', 'text'},
    'modalidade': {'char', 'text'},
    'situacao': {'char', 'text'},
    'valor_estimado': {'float', 'monetary'},
    'data_publicacao': {'date'},
    'data_atualizacao': {'date'},
    'data_encerramento': {'datetime'},
    'url_origem': {'char', 'text'},
    'assinatura': {'char', 'text'},
}


class OdooClient:
    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
    ):
        settings.require_odoo()

        self.cfg = settings
        self.session = session or requests.Session()
        self.base = settings.odoo_url

        self.headers = {
            'Authorization': f'bearer {settings.odoo_api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

        if settings.odoo_database:
            self.headers['X-Odoo-Database'] = settings.odoo_database

    def call(self, model: str, method: str, **params):
        if (
            model not in {'project.task', 'project.project'}
            or method not in {
                'fields_get',
                'search_read',
                'create',
                'write',
            }
        ):
            raise ValueError('Operation not permitted')

        try:
            res = self.session.post(
                f'{self.base}/json/2/{model}/{method}',
                headers=self.headers,
                json=params,
                timeout=(10, 40),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                'Remote service connection failed'
            ) from exc

        if not res.ok:
            raise RuntimeError(
                f'Remote request failed: '
                f'{model}/{method} HTTP {res.status_code}'
            )

        try:
            return res.json()
        except ValueError as exc:
            raise RuntimeError(
                'Invalid response format'
            ) from exc

    def fields(self, model: str) -> dict:
        result = self.call(
            model,
            'fields_get',
            attributes=[
                'type',
                'string',
                'readonly',
                'required',
            ],
        )

        if not isinstance(result, dict):
            raise ValueError(
                'Unexpected field metadata'
            )

        return result

    def check_fields(self, write: bool = False) -> dict:
        task = self.fields('project.task')
        project = self.fields('project.project')

        def valid(
            spec: dict,
            name: str,
            types: set,
            label: str,
        ):
            if not name or name not in spec:
                raise ValueError(
                    f'{label}: required field is missing'
                )

            meta = spec[name]

            if (
                meta.get('type') not in types
                or (write and meta.get('readonly'))
            ):
                raise ValueError(
                    f'{label}: incompatible field configuration'
                )

        if write:
            valid(
                task,
                self.cfg.pncp_field,
                FIELD_TYPES['pncp'],
                'identifier',
            )

            valid(
                project,
                self.cfg.checkpoint_field,
                FIELD_TYPES['checkpoint'],
                'checkpoint',
            )

            if 'project_id' not in task or 'name' not in task:
                raise ValueError(
                    'Required base fields are missing'
                )

            if self.cfg.stage_id and 'stage_id' not in task:
                raise ValueError(
                    'Required stage field is missing'
                )

            for source, dest in self.cfg.field_map.items():
                if source not in ALLOWED_FIELDS:
                    raise ValueError(
                        'Unsupported source field'
                    )

                valid(
                    task,
                    dest,
                    FIELD_TYPES[source],
                    source,
                )

        return {
            'project.task': task,
            'project.project': project,
        }

    def project(self, fields: list[str]) -> dict:
        rows = self.call(
            'project.project',
            'search_read',
            domain=[
                ['id', '=', self.cfg.project_id],
            ],
            fields=['id', *fields],
            limit=2,
        )

        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError(
                'Requested resource is unavailable'
            )

        return rows[0]

    def has_unlinked_tasks(self) -> bool:
        rows = self.call(
            'project.task',
            'search_read',
            domain=[
                ['project_id', '=', self.cfg.project_id],
                [self.cfg.pncp_field, '=', False],
            ],
            fields=['id'],
            limit=1,
        )

        if not isinstance(rows, list):
            raise RuntimeError(
                'Unexpected lookup response'
            )

        return bool(rows)

    def tasks(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}

        fields = [
            'id',
            self.cfg.pncp_field,
            *self.cfg.field_map.values(),
        ]

        rows = self.call(
            'project.task',
            'search_read',
            domain=[
                ['project_id', '=', self.cfg.project_id],
                [self.cfg.pncp_field, 'in', ids],
            ],
            fields=list(dict.fromkeys(fields)),
            limit=len(ids) + 1,
        )

        if not isinstance(rows, list):
            raise RuntimeError(
                'Unexpected lookup response'
            )

        by_id = {}

        for item in rows:
            pncp_id = item.get(self.cfg.pncp_field)

            if pncp_id in by_id:
                raise RuntimeError(
                    'Duplicate record identifier'
                )

            by_id[pncp_id] = item

        return by_id

    def create_task(self, values: dict) -> None:
        result = self.call(
            'project.task',
            'create',
            vals_list=[values],
        )

        if (
            not isinstance(result, (int, list))
            or isinstance(result, bool)
            or (
                isinstance(result, list)
                and (
                    len(result) != 1
                    or not isinstance(result[0], int)
                )
            )
        ):
            raise RuntimeError(
                'Unexpected create response'
            )

    def update_task(
        self,
        task_id: int,
        values: dict,
    ) -> None:
        result = self.call(
            'project.task',
            'write',
            ids=[task_id],
            vals=values,
        )

        if result is not True:
            raise RuntimeError(
                'Update was not confirmed'
            )

    def write_checkpoint(self, serialized: str) -> None:
        result = self.call(
            'project.project',
            'write',
            ids=[self.cfg.project_id],
            vals={
                self.cfg.checkpoint_field: serialized,
            },
        )

        if result is not True:
            raise RuntimeError(
                'State update was not confirmed'
            )
