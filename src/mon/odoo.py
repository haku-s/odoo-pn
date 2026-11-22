"""Odoo JSON-2 client and destination preflight."""

from __future__ import annotations

from typing import Any, TypeGuard

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests

from .config import FIELD_ALIASES, OFFICIAL_FIELDS, OPTIONAL_OFFICIAL_FIELDS, TECHNICAL_FIELD, Settings
from .relevance import fold

TASK = 'project.task'
PROJECT = 'project.project'
STAGE = 'project.task.type'
ATTACHMENT = 'ir.attachment'
MESSAGE = 'mail.message'
CURRENCY = 'res.currency'
COMPANY = 'res.company'
MODEL_FIELD = 'ir.model.fields'
CONTEXT = {'lang': 'pt_BR', 'active_test': False}
TEXT = {'char', 'text'}

FORM = (
    ('Projeto', 'project', 'configuration', {'many2one'}, 'project_id'),
    ('Responsáveis', None, None, None, None),
    ('Número do processo', 'process_number', 'processo', TEXT, None),
    ('Número da licitação', 'purchase_number', 'numeroCompra', TEXT, None),
    ('Edital / aviso', 'notice_number', 'official notice number', TEXT, None),
    ('Modalidade', 'modality', 'modalidadeNome', TEXT | {'selection'}, None),
    ('Tipo de contratação', 'contract_type', 'modalidadeId / modoDisputaNome', {'selection'}, None),
    ('Critério de julgamento', 'judgment_criterion', 'itens.criterioJulgamentoNome',
     {'selection'}, 'x_studio_criterio_de_julgamento_1'),
    ('Forma de adjudicação', 'award_basis', 'explicit scope in official notice', {'selection'}, None),
    ('Valor estimado', 'estimated_value', 'valorTotalEstimado', {'float', 'monetary'}, None),
    ('Tags', None, None, None, None),
    ('Prazo final', 'deadline', 'dataEncerramentoProposta', {'datetime'}, 'date_deadline'),
    ('Data e hora da sessão', 'session_datetime', 'explicit session time in official notice', {'datetime'}, None),
    ('Prazo para esclarecimentos', 'clarification_deadline',
     'explicit deadline in official notice', {'datetime'}, None),
    ('Órgão contratante', 'organization', 'orgaoEntidade.razaoSocial', TEXT, None),
    ('Município / UF', 'municipality_state',
     'unidadeOrgao.municipioNome / unidadeOrgao.ufSigla', TEXT, None),
    ('Identificador PNCP', 'identifier', 'numeroControlePNCP', TEXT, None),
    ('Plataforma de disputa', 'dispute_platform', 'linkSistemaOrigem', TEXT | {'selection'}, None),
    ('Link oficial do processo', 'pncp_link', 'numeroControlePNCP → PNCP public page', TEXT, None),
    ('Resultado da Homologação', None, None, None, None),
)

OPERATIONS = {
    TASK: {'fields_get', 'search_read', 'search_count', 'default_get', 'has_access', 'create', 'write'},
    PROJECT: {'fields_get', 'search_read', 'has_access', 'write'},
    STAGE: {'fields_get', 'search_read'},
    ATTACHMENT: {'fields_get', 'search_read', 'has_access', 'create'},
    MESSAGE: {'fields_get', 'search_read', 'has_access'},
    CURRENCY: {'fields_get', 'search_read'},
    COMPANY: {'search_read'},
    MODEL_FIELD: {'search_read'},
}
ATTACHMENT_FIELDS = {
    'name': {'char'}, 'description': {'text'}, 'checksum': {'char'},
    'res_model': {'char'}, 'res_id': {'many2one_reference', 'integer'},
    'res_field': {'char'}, 'type': {'selection'}, 'raw': {'binary'}, 'mimetype': {'char'},
}
ATTACHMENT_READ = ['name', 'description', 'checksum', 'res_model', 'res_id', 'res_field', 'type']
MAX_ODOO_ROWS = 1000
CHECKSUM = re.compile(r'[0-9a-f]{40}')


def selection_keys(meta: dict[str, Any]) -> dict[str, Any]:
    choices = meta.get('selection')
    if not isinstance(choices, list) or not choices:
        raise ValueError('Missing selection options')
    keys = {}
    for pair in choices:
        if (
            not isinstance(pair, (list, tuple)) or len(pair) != 2
            or not isinstance(pair[0], str) or not pair[0] or pair[0] in keys
            or not isinstance(pair[1], str) or not pair[1].strip()
        ):
            raise ValueError('Invalid selection options')
        keys[pair[0]] = pair[1]
    return keys


def selection_value(meta: dict[str, Any], value: str, *, required: bool = True) -> str | None:
    keys = [
        key for key, label in selection_keys(meta).items()
        if fold(label.strip()) == fold(value)
    ]
    if len(keys) != 1:
        if not required:
            return None
        raise ValueError('Official modality has no unique Odoo selection')
    return keys[0]


def amount_value(value: float, step: Decimal | None) -> float:
    if step is None:
        return value
    try:
        return float((Decimal(str(value)) / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step)
    except InvalidOperation as exc:
        raise ValueError('Estimated value exceeds the supported Odoo precision') from exc


def float_step(meta: dict[str, Any]) -> Decimal | None:
    digits = meta.get('digits')
    if digits is None or digits is False or digits == 0:
        return None
    if (
        not isinstance(digits, (list, tuple)) or len(digits) != 2
        or any(type(value) is not int for value in digits)
        or not 0 <= digits[1] <= 16
    ):
        raise ValueError('Invalid Odoo estimated value precision')
    return Decimal(1).scaleb(-digits[1])


def positive_id(value: object) -> TypeGuard[int]:
    return type(value) is int and value > 0


def relation_id(value):
    if isinstance(value, (list, tuple)) and len(value) == 2 and positive_id(value[0]):
        return value[0]
    return None


def _valid(metadata, name, types, *, writable=False, relation=None):
    meta = metadata.get(name, {})
    if (
        meta.get('type') not in types
        or (writable and meta.get('readonly') is not False)
        or (relation and meta.get('relation') != relation)
    ):
        raise ValueError('Incompatible or inaccessible Odoo field metadata')


def field_inventory(task: dict[str, Any], settings: Settings) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    inventory, mappings, errors = [], {}, []
    configured = {}
    for key, name in settings.field_map.items():
        source = FIELD_ALIASES.get(key, key)
        if source not in OFFICIAL_FIELDS:
            errors.append('Unsupported Odoo field mapping')
        if source in configured:
            errors.append('Duplicate Odoo source mapping')
        configured[source] = name

    for label, source, path, types, standard in FORM:
        matches = [
            (name, meta) for name, meta in task.items()
            if fold(str(meta.get('string') or '').strip()) == fold(label)
        ]
        if source == 'judgment_criterion':
            matches = [(name, meta) for name, meta in matches if name == standard]
        name, meta = matches[0] if len(matches) == 1 else (None, {})
        entry = {
            'label': label, 'technical_name': name, 'type': meta.get('type'),
            'selections': meta.get('selection') or [], 'source_path': path,
            'writable': None if type(meta.get('readonly')) is not bool else not meta['readonly'],
            'write_enabled': False,
            'status': 'unresolved' if not matches else 'ambiguous' if len(matches) > 1 else 'excluded',
        }
        inventory.append(entry)
        if source is None:
            continue
        required = source not in OPTIONAL_OFFICIAL_FIELDS or source in configured
        if name is None:
            if required:
                errors.append('Missing or ambiguous required Odoo field')
            continue
        expected = settings.pncp_field if source == 'identifier' else configured.get(source)
        if (
            (expected and name != expected)
            or (standard and name != standard)
            or (not standard and (not name.startswith('x_') or not TECHNICAL_FIELD.fullmatch(name)))
        ):
            entry['status'] = 'incompatible'
            if required:
                errors.append('Odoo field mapping does not match label')
            continue
        try:
            _valid(task, name, types, writable=True, relation=PROJECT if source == 'project' else None)
            if meta['type'] == 'selection':
                selection_keys(meta)
            if source == 'estimated_value':
                if meta['type'] == 'monetary':
                    currency = meta.get('currency_field')
                    if not isinstance(currency, str):
                        raise ValueError('Missing Odoo currency field')
                    _valid(task, currency, {'many2one'}, relation=CURRENCY)
                else:
                    float_step(meta)
        except ValueError:
            entry['status'] = 'incompatible'
            if required:
                errors.append('Incompatible Odoo field metadata')
            continue
        entry['status'] = 'validated'
        if source not in ('project', 'identifier'):
            mappings[source] = (name, meta)
    return inventory, mappings, errors


class OdooClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        settings.require_odoo()
        self.cfg = settings
        self.session = session or requests.Session()
        self.base = settings.odoo_url
        self.mappings = {}
        self.currency_steps: dict[int, Decimal] = {}
        self._task_currency_defaults: dict[str, Any] | None = None
        self._assignable_task_currency: int | None = None
        self.currency_amount_siblings: tuple[str, ...] | None = None
        self.headers = {
            'Authorization': f'bearer {settings.odoo_api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        if settings.odoo_database:
            self.headers['X-Odoo-Database'] = settings.odoo_database

    def call(self, model: str, method: str, **params):
        if method not in OPERATIONS.get(model, ()):
            raise ValueError('Operation not permitted')
        try:
            response = self.session.post(
                f'{self.base}/json/2/{model}/{method}', headers=self.headers,
                json=params, timeout=(10, 40), allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f'Odoo {model}/{method}: connection failed') from exc
        if response.status_code != 200:
            raise RuntimeError(f'Odoo {model}/{method}: HTTP {response.status_code}')
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError('Invalid Odoo response format') from exc

    def _require_attachment_contract(self) -> None:
        try:
            response = self.session.get(
                f'{self.base}/web/version', headers={'Accept': 'application/json'},
                timeout=(10, 20), allow_redirects=False,
            )
            if response.status_code != 200:
                raise RuntimeError(f'Odoo version check: HTTP {response.status_code}')
            body = response.json()
        except requests.RequestException as exc:
            raise RuntimeError('Odoo version check could not connect') from exc
        except ValueError as exc:
            raise RuntimeError('Invalid Odoo version response') from exc
        info = body.get('version_info') if isinstance(body, dict) else None
        if not isinstance(info, list) or not info or type(info[0]) is not int or info[0] != 20:
            raise ValueError('The attachment write contract requires Odoo 20')

    def fields(self, model: str) -> dict[str, Any]:
        result = self.call(
            model, 'fields_get', context=CONTEXT,
            attributes=['type', 'string', 'readonly', 'selection', 'relation', 'digits', 'currency_field'],
        )
        if not isinstance(result, dict) or any(
            not isinstance(name, str) or not isinstance(meta, dict)
            for name, meta in result.items()
        ):
            raise ValueError('Unexpected Odoo field metadata')
        return result

    def rows(self, model, domain, fields, limit):
        rows = self.call(
            model, 'search_read', domain=domain, fields=['id', *fields],
            limit=limit, context=CONTEXT,
        )
        if (
            not isinstance(rows, list) or len(rows) >= limit
            or any(not isinstance(row, dict) or not positive_id(row.get('id')) for row in rows)
            or len({row['id'] for row in rows}) != len(rows)
        ):
            raise RuntimeError(f'Incomplete or invalid lookup: {model}')
        return rows

    def require_access(self, model, operation, ids=()):
        if self.call(model, 'has_access', ids=list(ids), operation=operation, context=CONTEXT) is not True:
            raise ValueError(f'Odoo access denied: {model}/{operation}')

    def check_fields(self, write=False, *, report=None) -> dict[str, Any]:
        task = self.fields(TASK)
        project = self.fields(PROJECT)
        inventory, mappings, errors = field_inventory(task, self.cfg)
        if report is not None:
            report.extend(inventory)
        if errors:
            raise ValueError('; '.join(dict.fromkeys(errors)))
        self.cfg.require_odoo(write=write)
        if not self.cfg.pncp_field:
            raise ValueError('Missing ODOO_PNCP_FIELD')
        if not self.cfg.stage_id or self.cfg.stage_id <= 0:
            raise ValueError('Missing or invalid ODOO_STAGE_ID')
        _valid(task, 'name', {'char'}, writable=write)
        if write:
            self._require_attachment_contract()
            _valid(task, 'stage_id', {'many2one'}, writable=True, relation=STAGE)
            _valid(project, self.cfg.checkpoint_field, {'text'}, writable=True)
        elif self.cfg.checkpoint_field:
            _valid(project, self.cfg.checkpoint_field, {'text'})

        stage = self.fields(STAGE)
        _valid(stage, 'name', {'char'})
        _valid(stage, 'active', {'boolean'})
        _valid(stage, 'project_ids', {'many2many'}, relation=PROJECT)
        _valid(project, 'name', {'char'})
        _valid(project, 'active', {'boolean'})

        project_row = self.project(['name', 'active', *(
            [self.cfg.checkpoint_field] if self.cfg.checkpoint_field else []
        )])
        if self.cfg.checkpoint_field and self.cfg.checkpoint_field not in project_row:
            raise ValueError('Odoo checkpoint could not be read')
        if fold(project_row.get('name')) != fold('Licitações') or project_row.get('active') is not True:
            raise ValueError('Configured Odoo project does not match the active destination')
        stages = self.rows(STAGE, [['id', '=', self.cfg.stage_id]],
                           ['name', 'active', 'project_ids'], 2)
        if (
            len(stages) != 1 or stages[0]['id'] != self.cfg.stage_id
            or fold(stages[0].get('name')) != fold('Capturados')
            or stages[0].get('active') is not True
            or not isinstance(stages[0].get('project_ids'), list)
            or self.cfg.project_id not in stages[0]['project_ids']
        ):
            raise ValueError('Configured Odoo initial stage does not belong to the destination')

        attachment = self.fields(ATTACHMENT)
        for name, types in ATTACHMENT_FIELDS.items():
            _valid(attachment, name, types, writable=write and name in {
                'name', 'description', 'type', 'raw',
            })
        if 'binary' not in selection_keys(attachment['type']):
            raise ValueError('Odoo binary attachment type is unavailable')
        # Attachment linkage and MIME can be API inputs even when readonly in the UI.
        messages = self.fields(MESSAGE)
        _valid(messages, 'model', {'char'})
        _valid(messages, 'res_id', {'many2one_reference', 'integer'})
        _valid(messages, 'attachment_ids', {'many2many'}, relation=ATTACHMENT)
        if mappings['estimated_value'][1]['type'] == 'monetary':
            amount_field, amount = mappings['estimated_value']
            self.currency_amount_siblings = tuple(
                name for name, meta in task.items() if name != amount_field
                and meta.get('type') == 'monetary' and meta.get('currency_field') == amount['currency_field']
            )
            currency = self.fields(CURRENCY)
            _valid(currency, 'name', {'char'})
            _valid(currency, 'rounding', {'float'})
        for model in (TASK, ATTACHMENT, MESSAGE):
            self.require_access(model, 'read')
        if write:
            self.require_access(TASK, 'create')
            self.require_access(TASK, 'write')
            self.require_access(PROJECT, 'write', [self.cfg.project_id])
            self.require_access(ATTACHMENT, 'create')
            for entry in inventory:
                entry['write_enabled'] = entry['status'] == 'validated'
        self.mappings = mappings
        return project_row

    def project(self, fields: list[str]) -> dict[str, Any]:
        rows = self.rows(PROJECT, [['id', '=', self.cfg.project_id]], fields, 2)
        if len(rows) != 1 or rows[0]['id'] != self.cfg.project_id:
            raise ValueError('Configured Odoo project is unavailable')
        return rows[0]

    def has_unlinked_tasks(self) -> bool:
        count = self.call(TASK, 'search_count', domain=[
            ['project_id', '=', self.cfg.project_id],
            [self.cfg.pncp_field, '=', False],
        ], limit=1, context=CONTEXT)
        if type(count) is not int or count not in (0, 1):
            raise RuntimeError('Invalid unlinked task count')
        return bool(count)

    def tasks(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        identifier = self.cfg.pncp_field
        fields = ['name', 'project_id', 'stage_id', identifier, *(name for name, _ in self.mappings.values())]
        amount = self.mappings.get('estimated_value')
        if amount and amount[1]['type'] == 'monetary':
            fields.append(amount[1]['currency_field'])
            fields.extend(name for name in self.currency_amount_siblings or () if name not in fields)
        rows = self.rows(TASK, [[identifier, 'in', ids]], fields, len(ids) + 1)
        by_id = {}
        for row in rows:
            key = row.get(identifier)
            if (
                not isinstance(key, str) or key not in ids or key in by_id
                or relation_id(row.get('project_id')) != self.cfg.project_id
                or any(field not in row for field in fields)
            ):
                raise RuntimeError('Duplicate identifier or invalid task association')
            by_id[key] = row
        return by_id

    def currency_step(self, currency_relation) -> Decimal:
        currency_id = currency_relation if positive_id(currency_relation) else relation_id(currency_relation)
        if currency_id is None:
            raise ValueError('Task currency is unavailable for the estimated value')
        if currency_id not in self.currency_steps:
            rows = self.rows(CURRENCY, [['id', '=', currency_id]], ['name', 'rounding'], 2)
            if len(rows) != 1 or rows[0]['id'] != currency_id or rows[0].get('name') != 'BRL':
                raise ValueError('Task currency does not match the PNCP estimate')
            rounding = rows[0].get('rounding')
            if type(rounding) not in (int, float):
                raise ValueError('Invalid Odoo currency rounding')
            step = Decimal(str(rounding))
            if not step.is_finite() or step <= 0:
                raise ValueError('Invalid Odoo currency rounding')
            self.currency_steps[currency_id] = step
        return self.currency_steps[currency_id]

    def _task_creation_context(self) -> dict[str, Any]:
        return {**CONTEXT, 'default_project_id': self.cfg.project_id, 'default_stage_id': self.cfg.stage_id}

    def task_currency_default(self) -> int | None:
        field = self.mappings['estimated_value'][1]['currency_field']
        if self._task_currency_defaults is None:
            defaults = self.call(
                TASK, 'default_get', fields=[field, *(self.currency_amount_siblings or ())],
                context=self._task_creation_context(),
            )
            currency_id = defaults.get(field) if isinstance(defaults, dict) else None
            if not isinstance(defaults, dict) or (currency_id is not None and currency_id is not False
                                                   and not positive_id(currency_id)):
                raise ValueError('Invalid task currency default')
            self._task_currency_defaults = defaults
        return self._task_currency_defaults.get(field) or None

    def assignable_task_currency(self, task: dict[str, Any] | None = None) -> int:
        field = self.mappings['estimated_value'][1]['currency_field']
        siblings = self.currency_amount_siblings
        if siblings is None or not field.startswith('x_'):
            raise ValueError('Estimated value requires a writable independent task currency field')
        values = self._task_currency_defaults if task is None else task
        if siblings and (values is None or any(values.get(name) not in (None, 0) for name in siblings)):
            raise ValueError('Cannot set shared task currency with other monetary values')
        if self._assignable_task_currency is None:
            attributes = {
                'name': field, 'state': 'manual', 'ttype': 'many2one', 'relation': CURRENCY,
                'readonly': False, 'store': True, 'related': False, 'compute': False,
                'company_dependent': False,
            }
            rows = self.rows(MODEL_FIELD, [['model', '=', TASK], ['name', '=', field]], list(attributes), 2)
            if len(rows) != 1 or any(rows[0].get(key) != value for key, value in attributes.items()):
                raise ValueError('Estimated value requires a writable independent task currency field')
            rows = self.rows(CURRENCY, [['name', '=', 'BRL'], ['active', '=', True]], ['name'], 2)
            if len(rows) != 1 or rows[0].get('name') != 'BRL':
                raise ValueError('Unique active BRL currency was not found')
            brl = rows[0]['id']
            if siblings:
                company = relation_id(self.project(['company_id']).get('company_id'))
                if company is None:
                    raise ValueError('The project company currency could not be verified')
                companies = self.rows(COMPANY, [['id', '=', company]], ['currency_id'], 2)
                if (len(companies) != 1 or companies[0]['id'] != company
                        or relation_id(companies[0].get('currency_id')) != brl):
                    raise ValueError('A shared task currency requires BRL in the project company')
            self._assignable_task_currency = brl
        currency_id = self._assignable_task_currency
        if currency_id is None:
            raise RuntimeError('BRL currency could not be established')
        return currency_id

    def create(self, model: str, values: dict[str, Any]) -> int:
        params: dict[str, Any] = {'vals_list': [values]}
        if model == TASK:
            params['context'] = self._task_creation_context()
        result = self.call(model, 'create', **params)
        if isinstance(result, list) and len(result) == 1:
            result = result[0]
        if not positive_id(result):
            raise RuntimeError('Unexpected Odoo create response')
        return result

    def create_task(self, values: dict[str, Any]) -> int:
        return self.create(TASK, values)

    def update_task(self, task_id: int, values: dict[str, Any]) -> None:
        self.require_access(TASK, 'write', [task_id])
        if self.call(TASK, 'write', ids=[task_id], vals=values, context=CONTEXT) is not True:
            raise RuntimeError('Odoo task update was not confirmed')

    def attachments(self, task_id: int) -> list[dict[str, Any]]:
        direct = self.rows(ATTACHMENT, [
            ['res_model', '=', TASK], ['res_id', '=', task_id],
            ['type', '=', 'binary'], ['res_field', '=', False],
        ], ATTACHMENT_READ, MAX_ODOO_ROWS)
        messages = self.rows(MESSAGE, [
            ['model', '=', TASK], ['res_id', '=', task_id], ['attachment_ids', '!=', False],
        ], ['attachment_ids'], MAX_ODOO_ROWS)
        linked = {row['id']: row for row in direct}
        for row in direct:
            if not direct_attachment(row, task_id):
                raise RuntimeError('Invalid direct attachment association')
        ids = set()
        for message in messages:
            items = message.get('attachment_ids')
            if not isinstance(items, list) or not all(map(positive_id, items)):
                raise RuntimeError('Invalid Chatter attachment metadata')
            ids.update(items)
        if len(ids) >= MAX_ODOO_ROWS:
            raise RuntimeError('Chatter attachment limit exceeded')
        missing = sorted(ids - linked.keys())
        for offset in range(0, len(missing), 100):
            chunk = missing[offset:offset + 100]
            rows = self.rows(ATTACHMENT, [['id', 'in', chunk]], ATTACHMENT_READ, len(chunk) + 1)
            if (
                {row['id'] for row in rows} != set(chunk)
                or any(row.get('type') not in {'binary', 'url'} for row in rows)
            ):
                raise RuntimeError('Incomplete Chatter attachment lookup')
            linked.update((row['id'], row) for row in rows if row.get('type') == 'binary')
        if any(
            not isinstance(row.get('name'), str)
            or not isinstance(row.get('checksum'), str)
            or not CHECKSUM.fullmatch(row['checksum'])
            or 'description' not in row
            for row in linked.values()
        ):
            raise RuntimeError('Incomplete Odoo attachment metadata')
        return list(linked.values())

    def write_checkpoint(self, serialized: str) -> None:
        result = self.call(
            PROJECT, 'write', ids=[self.cfg.project_id],
            vals={self.cfg.checkpoint_field: serialized}, context=CONTEXT,
        )
        if result is not True:
            raise RuntimeError('Odoo checkpoint update was not confirmed')


def direct_attachment(row: dict[str, Any], task_id: int) -> bool:
    return (
        row.get('res_model') == TASK and row.get('res_id') == task_id
        and row.get('type') == 'binary' and row.get('res_field') is False
    )
