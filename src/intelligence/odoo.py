"""Odoo JSON-2 client for the data sync models."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Collection
from datetime import datetime, timezone
from time import sleep
from typing import Any, TypeGuard
from xml.etree import ElementTree

import requests
from urllib3.exceptions import InvalidHeader
from urllib3.util import Retry

FONTES = 'x_fontes'
ASSUNTOS = 'x_assuntos'
PUBLICACOES = 'x_publicacoes'
MODEL_FIELD = 'ir.model.fields'
FUNDING = 'x_oportunidade_fomento'
FUNDING_REVISION = 'x_revisao_fomento'

CONTEXT = {'lang': 'pt_BR', 'active_test': False}
TEXT = {'char', 'text'}
# Every property consumed by the validation logic must be requested explicitly.
FIELD_ATTRIBUTES = ['type', 'string', 'readonly', 'selection', 'relation', 'related', 'store', 'required', 'relation_field']
SCIENCE_TYPE = 'scientific_publication'
NEWS_PUBLICATION = ['x_studio_tipo_de_publicacao', '!=', SCIENCE_TYPE]
SELECTIONS = {
    'x_studio_metodo_de_coleta': ('RSS', 'API REST', 'XML'),
    'x_studio_tipo_de_publicacao': (
        'Notícia', 'Ato oficial', 'Matéria legislativa',
        'Contrato público', 'Divulgação estatística',
    ),
    'x_studio_area': ('Corporativo', 'Educação', 'Saúde', 'Transversal'),
}

FONTES_FIELDS = {
    'x_name': {'char'},
    'x_studio_codigo_da_fonte_1': TEXT,
    'x_studio_url': {'char'},
    'x_studio_metodo_de_coleta': {'selection'},
    'x_studio_coleta_habilitada': {'boolean'},
    'x_studio_ultima_coleta_bem_sucedida_1': {'datetime'},
    'x_studio_ultimo_erro_1': {'text'},
}
ASSUNTOS_FIELDS = {
    'x_name': {'char'},
    'x_studio_area': {'selection'},
    'x_studio_termos_de_inclusao': {'text'},
    'x_studio_termos_de_exclusao': {'text'},
}
PUBLICACOES_FIELDS = {
    'x_name': {'char'},
    'x_studio_fontes': {'many2one'},
    'x_studio_assunto_1': {'many2one'},
    'x_studio_identificador_externo': TEXT,
    'x_studio_url': {'char'},
    'x_studio_tipo_de_publicacao': {'selection'},
    'x_studio_data_de_publicacao': {'datetime'},
    'x_studio_resumo': {'text'},
    'x_studio_primeira_coleta': {'datetime'},
    'x_studio_ultima_atualizacao': {'datetime'},
}
# Related, read-only: must never receive a value.
READONLY_RELATED = {'x_studio_area_1'}
# Full publication record used for change detection and write confirmation.
PUBLICACOES_LEITURA = [
    'x_name', 'x_studio_fontes', 'x_studio_identificador_externo', 'x_studio_url',
    'x_studio_tipo_de_publicacao', 'x_studio_data_de_publicacao', 'x_studio_assunto_1',
    'x_studio_resumo', 'x_studio_primeira_coleta', 'x_studio_ultima_atualizacao',
]
CAMPOS_MANY2ONE = {'x_studio_fontes', 'x_studio_assunto_1'}
PUBLICATION_IDENTITY = ('x_studio_fontes', 'x_studio_identificador_externo', 'x_studio_url')
AVATAR_RESPONSE_LIMIT = 8 * 1024 * 1024

OPERATIONS = {
    FONTES: {'fields_get', 'search_read', 'search_count', 'create', 'write', 'has_access'},
    ASSUNTOS: {'fields_get', 'search_read', 'search_count', 'has_access'},
    PUBLICACOES: {'fields_get', 'get_view', 'search_read', 'search_count', 'create', 'write', 'has_access'},
    MODEL_FIELD: {'search_read'},
    FUNDING: {'fields_get', 'search_read', 'has_access', 'create', 'write'},
    FUNDING_REVISION: {'fields_get', 'search_read', 'has_access'},
}

TECHNICAL = re.compile(r'^[a-z][a-z0-9_]*$')
# Operational lookups must ignore archived records.  The instance uses a custom
# boolean field (`x_active`), not the conventional `active` flag, so the filter
# has to be explicit in the domain.
DOMINIO_ATIVOS = [['x_active', '=', True]]


def positive_id(value: object) -> TypeGuard[int]:
    return type(value) is int and value > 0


def relation_id(value):
    if isinstance(value, (list, tuple)) and len(value) == 2 and positive_id(value[0]):
        return value[0]
    if type(value) is int and value > 0:
        return value
    return None


def normalizar_valor(nome: str, value):
    """Normalise a stored value for comparison with a submitted payload."""
    if nome in CAMPOS_MANY2ONE:
        return relation_id(value)
    if value is False or value is None or value == '':
        return None
    return value


def _valid(metadata: dict[str, Any], name: str, types: set[str], *, relation: str | None = None) -> dict[str, Any]:
    meta = metadata.get(name)
    if not isinstance(meta, dict) or meta.get('type') not in types:
        raise ValueError(f'Incompatible Odoo field metadata: {name}')
    if relation and meta.get('relation') != relation:
        raise ValueError(f'Incompatible Odoo relation metadata: {name}')
    return meta


def selection_keys(meta: dict[str, Any]) -> tuple[str, ...]:
    choices = meta.get('selection')
    if not isinstance(choices, list) or not choices:
        raise ValueError('Missing selection options')
    keys = []
    for pair in choices:
        if (
            not isinstance(pair, (list, tuple)) or len(pair) != 2
            or not isinstance(pair[0], str) or not pair[0]
            or not isinstance(pair[1], str) or not pair[1].strip()
        ):
            raise ValueError('Invalid selection options')
        keys.append(pair[0])
    if len(set(keys)) != len(keys):
        raise ValueError('Duplicate selection options')
    return tuple(keys)


def _binary(value: object) -> tuple[str, bytes]:
    if value is False:
        return 'false', b''
    if value is None:
        return 'null', b''
    if value == '':
        return 'empty_string', b''
    encoded = value.get('content') if isinstance(value, dict) else value
    if not isinstance(encoded, str) or len(encoded) > AVATAR_RESPONSE_LIMIT:
        raise RuntimeError('Unexpected Odoo Avatar binary value')
    try:
        data = base64.b64decode(encoded.replace('\r', '').replace('\n', ''), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError('Invalid Odoo Avatar Base64 value') from exc
    size = value.get('size') if isinstance(value, dict) else len(data)
    if not data or type(size) is not int or size != len(data):
        raise RuntimeError('Unexpected Odoo Avatar binary size')
    return ('base64_object' if isinstance(value, dict) else 'base64_string'), data


class OdooHTTPError(RuntimeError):
    def __init__(self, model: str, method: str, status_code: int, *, validation_rejected: bool = False):
        super().__init__(f'Odoo {model}/{method}: HTTP {status_code}')
        self.model = model
        self.method = method
        self.status_code = status_code
        self.validation_rejected = validation_rejected


def _validation_rejected(response: requests.Response) -> bool:
    if response.status_code != 422:
        return False
    try:
        error = response.json()
    except ValueError:
        return False
    return isinstance(error, dict) and error.get('name') in (
        'odoo.exceptions.UserError', 'odoo.exceptions.ValidationError',
    )


class OdooClient:
    def __init__(self, settings, session: requests.Session | None = None):
        settings.require_odoo()
        self.cfg = settings
        self.session = session or requests.Session()
        self.headers = {
            'Authorization': f'bearer {settings.odoo_api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        if settings.odoo_database:
            self.headers['X-Odoo-Database'] = settings.odoo_database
        self._metadata: dict[str, dict[str, Any]] = {}
        self._selections: dict[str, tuple[str, ...]] = {}
        self._avatar: tuple[str, bool] | None = None
        self._avatar_write_checked = False

    # ---------------------------------------------------------------- transport
    def call(self, model: str, method: str, *, response_limit: int | None = None, **params):
        if method not in OPERATIONS.get(model, ()):
            raise ValueError('Operation not permitted')
        try:
            retries = 0
            while True:
                response = self.session.post(
                    f'{self.cfg.odoo_url}/json/2/{model}/{method}',
                    headers=self.headers,
                    json=params,
                    timeout=self.cfg.request_timeout,
                    allow_redirects=False,
                    stream=response_limit is not None,
                )
                if response.status_code != 429 or retries == 2:
                    break
                wait = response.headers.get('Retry-After')
                try:
                    delay = max(1, Retry().parse_retry_after(wait)) if wait else 45
                except InvalidHeader:
                    break
                if delay > 60:
                    break
                response.close()
                retries += 1
                sleep(delay)
            if response_limit:
                with response:
                    if response.status_code != 200:
                        raise OdooHTTPError(model, method, response.status_code)
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        body.extend(chunk)
                        if len(body) > response_limit:
                            raise RuntimeError('Odoo response exceeds the permitted size')
                    return json.loads(body)
            if response.status_code != 200:
                raise OdooHTTPError(
                    model, method, response.status_code,
                    validation_rejected=method == 'create' and _validation_rejected(response),
                )
            return response.json()
        except requests.JSONDecodeError as exc:
            raise RuntimeError('Invalid Odoo response format') from exc
        except requests.RequestException as exc:
            raise RuntimeError(f'Odoo {model}/{method}: connection failed') from exc
        except ValueError as exc:
            raise RuntimeError('Invalid Odoo response format') from exc

    def fields(self, model: str) -> dict[str, Any]:
        if model not in self._metadata:
            context = {'active_test': False} if model in (FUNDING, FUNDING_REVISION) else CONTEXT
            result = self.call(
                model, 'fields_get', context=context, attributes=FIELD_ATTRIBUTES,
            )
            if not isinstance(result, dict) or any(
                not isinstance(name, str) or not isinstance(meta, dict)
                for name, meta in result.items()
            ):
                raise ValueError('Unexpected Odoo field metadata')
            self._metadata[model] = result
        return self._metadata[model]

    def selection(self, model: str, name: str) -> tuple[str, ...]:
        key = f'{model}.{name}'
        if key not in self._selections:
            self._selections[key] = selection_keys(self.fields(model)[name])
        return self._selections[key]

    def check_fields(self, model: str, fields: dict[str, str], *, write: bool, access: tuple[str, ...],
                     relations: dict[str, str] | None = None) -> None:
        metadata = self.fields(model)
        for name, kind in fields.items():
            field = _valid(metadata, name, {kind}, relation=(relations or {}).get(name))
            if write and (field.get('readonly') is True or field.get('related') or field.get('store') is not True):
                raise ValueError(f'Odoo field is not writable: {model}.{name}')
        for operation in access:
            if self.call(model, 'has_access', ids=[], operation=operation) is not True:
                raise ValueError(f'Odoo access denied: {model}/{operation}')

    def missing_permissions(self) -> dict[str, dict[str, Any]]:
        """Check the shared identity using model-level, read-only access probes."""
        missing: dict[str, list[str]] = {}
        unverified: dict[str, str] = {}
        for model, operations in (
            (FONTES, ('read', 'write')),
            (ASSUNTOS, ('read',)),
            (PUBLICACOES, ('read', 'create', 'write')),
            (FUNDING, ('read', 'create', 'write')),
            (FUNDING_REVISION, ('read', 'create')),
        ):
            try:
                for operation in operations:
                    allowed = self.call(model, 'has_access', ids=[], operation=operation)
                    if type(allowed) is not bool:
                        raise RuntimeError('Invalid Odoo access response')
                    if not allowed:
                        missing.setdefault(model, []).append(operation)
            except OdooHTTPError as exc:
                unverified[model] = f'HTTP {exc.status_code}'
            except RuntimeError:
                unverified[model] = 'No valid access response'
        return {'missing': missing, 'unverified': unverified}

    def storage_rows(self, model: str, domain: list[Any], fields: list[str], limit: int) -> list[dict[str, Any]]:
        result = self.call(model, 'search_read', domain=domain, fields=sorted(fields), limit=limit, order='id asc',
                           context={'active_test': False}, response_limit=8 * 1024 * 1024)
        if not isinstance(result, list) or len(result) > limit or any(
            not isinstance(row, dict) or not positive_id(row.get('id')) for row in result
        ):
            raise RuntimeError(f'Invalid Odoo storage page: {model}')
        return result

    def storage_write(self, model: str, record_id: int | None, values: dict[str, Any]) -> None:
        try:
            if record_id is None:
                self.call(model, 'create', vals_list=[values], response_limit=256 * 1024)
            else:
                self.call(model, 'write', ids=[record_id], vals=values, response_limit=256 * 1024)
        except RuntimeError:
            # A lost response can follow a commit. The caller reads back before deciding the outcome.
            pass

    # ------------------------------------------------------- metadata checking
    def check(self, write: bool = False) -> None:
        """Validate the live metadata for the three data sync models."""
        for model, expected in (
            (FONTES, FONTES_FIELDS),
            (ASSUNTOS, ASSUNTOS_FIELDS),
            (PUBLICACOES, PUBLICACOES_FIELDS),
        ):
            meta = self.fields(model)
            for name, types in expected.items():
                if name not in meta:
                    raise ValueError(f'Missing Odoo field: {model}.{name}')
                _valid(meta, name, types)
            for name, keys in SELECTIONS.items():
                if name in expected:
                    actual = selection_keys(meta[name])
                    accepted = set(keys)
                    if name == 'x_studio_tipo_de_publicacao' and SCIENCE_TYPE in actual:
                        accepted.add(SCIENCE_TYPE)
                    if set(actual) != accepted:
                        raise ValueError(f'Unexpected selection keys: {model}.{name}')
        # The related Area must exist and must not be writable.
        public = self.fields(PUBLICACOES)
        area = _valid(public, 'x_studio_area_1', {'selection'})
        if area.get('related') != 'x_studio_assunto_1.x_studio_area':
            raise ValueError('Unexpected Area related field')
        if area.get('readonly') is not True or area.get('store') is not False:
            raise ValueError('Area related field is not read-only')
        _valid(public, 'x_studio_fontes', {'many2one'}, relation=FONTES)
        _valid(public, 'x_studio_assunto_1', {'many2one'}, relation=ASSUNTOS)
        if write:
            for model, fields in (
                (FONTES, FONTES_FIELDS),
                (PUBLICACOES, PUBLICACOES_FIELDS),
            ):
                meta = self.fields(model)
                for name in fields:
                    if meta[name].get('readonly') is True:
                        raise ValueError(f'Odoo field is read-only: {model}.{name}')
            for model in (FONTES, PUBLICACOES):
                if self.call(model, 'has_access', ids=[], operation='write', context=CONTEXT) is not True:
                    raise ValueError(f'Odoo access denied: {model}/write')
            if self.call(PUBLICACOES, 'has_access', ids=[], operation='create', context=CONTEXT) is not True:
                raise ValueError('Odoo access denied: create')

    # ------------------------------------------------------------------- reads
    def _registros(self, model: str, domain, fields: list[str], limite: int, rotulo: str) -> list[dict[str, Any]]:
        rows = self.call(
            model, 'search_read', domain=domain, fields=['id', *fields],
            limit=limite, context=CONTEXT,
        )
        if not isinstance(rows, list) or len(rows) >= limite:
            raise RuntimeError(f'Incomplete or oversized Odoo listing: {rotulo}')
        return rows

    def fontes(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        fields = ['x_name', 'x_studio_codigo_da_fonte_1', 'x_studio_url',
                  'x_studio_metodo_de_coleta', 'x_studio_coleta_habilitada']
        if include_archived:
            fields.append('x_active')
        rows = self._registros(
            FONTES, [] if include_archived else DOMINIO_ATIVOS, fields, 1000, 'sources',
        )
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not positive_id(row.get('id')):
                raise RuntimeError('Invalid Odoo source record')
            if include_archived and type(row.get('x_active')) is not bool:
                raise RuntimeError('Missing Odoo source active flag')
            code = row.get('x_studio_codigo_da_fonte_1')
            if not isinstance(code, str) or not code.strip() or not TECHNICAL.fullmatch(code):
                raise RuntimeError('Invalid Odoo source code')
            if code in seen:
                raise RuntimeError('Duplicate Odoo source code')
            seen.add(code)
            if row.get('x_studio_metodo_de_coleta') not in SELECTIONS['x_studio_metodo_de_coleta']:
                raise RuntimeError('Invalid Odoo collection method')
            result.append(row)
        return result

    def assuntos(self) -> list[dict[str, Any]]:
        rows = self._registros(
            ASSUNTOS, DOMINIO_ATIVOS,
            ['x_name', 'x_studio_area', 'x_studio_termos_de_inclusao',
             'x_studio_termos_de_exclusao'],
            1000, 'subjects',
        )
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not positive_id(row.get('id')):
                raise RuntimeError('Invalid Odoo subject record')
            name = row.get('x_name')
            if not isinstance(name, str) or not name.strip() or name in seen:
                raise RuntimeError('Invalid or duplicate Odoo subject')
            seen.add(name)
            if row.get('x_studio_area') not in SELECTIONS['x_studio_area']:
                raise RuntimeError('Invalid Odoo subject area')
            result.append(row)
        return result

    def publicacoes_por_identificador(self, fonte_id: int, identificadores: list[str]) -> dict[str, dict[str, Any]]:
        if not identificadores:
            return {}
        rows = self.call(
            PUBLICACOES, 'search_read',
            domain=[NEWS_PUBLICATION, ['x_studio_fontes', '=', fonte_id],
                     ['x_studio_identificador_externo', 'in', identificadores]],
            limit=len(identificadores) + 1, context=CONTEXT,
            fields=['id', *PUBLICACOES_LEITURA],
        )
        if not isinstance(rows, list) or len(rows) > len(identificadores):
            raise RuntimeError('Invalid Odoo publication lookup')
        found = {}
        for row in rows:
            if not isinstance(row, dict) or not positive_id(row.get('id')):
                raise RuntimeError('Invalid Odoo publication record')
            if row.get('x_studio_tipo_de_publicacao') == SCIENCE_TYPE:
                raise RuntimeError('Unexpected scientific publication in news lookup')
            if relation_id(row.get('x_studio_fontes')) != fonte_id:
                raise RuntimeError('Odoo publication belongs to another source')
            key = row.get('x_studio_identificador_externo')
            if not isinstance(key, str) or key in found:
                raise RuntimeError('Duplicate Odoo publication identifier')
            encontrado = {'id': row['id']}
            for nome in PUBLICACOES_LEITURA:
                encontrado[nome] = normalizar_valor(nome, row.get(nome))
            found[key] = encontrado
        return found

    def publicacoes_por_urls(self, source_ids: Collection[int], urls: Collection[str]) -> set[str]:
        if not source_ids or not urls:
            return set()
        if not all(positive_id(value) for value in source_ids) or not all(
            isinstance(url, str) and url for url in urls
        ):
            raise ValueError('Invalid Odoo publication URL lookup')
        source_set, url_set = set(source_ids), set(urls)
        rows = self._registros(
            PUBLICACOES,
            [NEWS_PUBLICATION, ['x_studio_fontes', 'in', sorted(source_set)], ['x_studio_url', 'in', sorted(url_set)]],
            ['x_studio_fontes', 'x_studio_url'], 1000, 'publication URLs',
        )
        found = set()
        for row in rows:
            if not isinstance(row, dict) or not positive_id(row.get('id')):
                raise RuntimeError('Invalid Odoo publication record')
            url = row.get('x_studio_url')
            if (not isinstance(url, str) or url not in url_set
                    or relation_id(row.get('x_studio_fontes')) not in source_set):
                raise RuntimeError('Unexpected Odoo publication URL match')
            found.add(url)
        return found

    # ------------------------------------------------------------------ avatar
    def _avatar_schema(self) -> tuple[str, bool]:
        if self._avatar is not None:
            return self._avatar
        metadata = self.fields(PUBLICACOES)
        view = self.call(PUBLICACOES, 'get_view', view_type='form', context=CONTEXT, response_limit=1024 * 1024)
        arch = view.get('arch') if isinstance(view, dict) and view.get('model') == PUBLICACOES else None
        if not isinstance(arch, str) or '<!DOCTYPE' in arch:
            raise ValueError('Invalid Odoo publication form')
        try:
            form = ElementTree.fromstring(arch)
        except ElementTree.ParseError as exc:
            raise ValueError('Invalid Odoo publication form XML') from exc
        if form.tag != 'form':
            raise ValueError('Unexpected Odoo publication view type')
        candidates: dict[str, bool] = {}
        nodes = list(form)
        while nodes:
            node = nodes.pop()
            if node.tag != 'field':
                nodes.extend(node)
                continue
            name = node.get('name', '')
            meta = metadata.get(name, {})
            label = node.get('string', meta.get('string'))
            if node.get('widget') != 'image' or not isinstance(label, str) or label.strip().casefold() != 'avatar':
                continue
            editable = node.get('readonly', 'False') in ('False', '0')
            candidates[name] = candidates.get(name, False) or editable
        if len(candidates) != 1:
            raise ValueError('Publication form does not identify a unique Avatar image widget')
        field, editable = next(iter(candidates.items()))
        if not TECHNICAL.fullmatch(field):
            raise ValueError('Invalid Odoo Avatar field name')
        meta = _valid(metadata, field, {'binary'})
        if meta.get('store') is not True or meta.get('related'):
            raise ValueError('Odoo Avatar must be a stored, independent binary field')
        self._avatar = field, editable and meta.get('readonly') is False
        return self._avatar

    def avatar_field(self, *, write: bool = False) -> str | None:
        configured = getattr(self.cfg, 'avatar_field', None)
        if write and not configured:
            return None
        field, editable = self._avatar_schema()
        if configured and configured != field:
            raise ValueError('Configured field does not match the publication form Avatar')
        if write and not self._avatar_write_checked:
            if not editable or not self._avatar_access([]):
                raise ValueError('Odoo Avatar write access was not verified')
            self._avatar_write_checked = True
        return field

    def _require_avatar(self, field: str, *, write: bool = False) -> None:
        if not field or self.avatar_field(write=write) != field:
            raise ValueError('Odoo Avatar is not enabled')

    def _avatar_access(self, ids: list[int]) -> bool:
        result = self.call(PUBLICACOES, 'has_access', ids=ids, operation='write', context=CONTEXT)
        if type(result) is not bool:
            raise RuntimeError('Invalid Odoo Avatar access response')
        return result

    def _avatar_domain(self, field: str, *, filled: bool = False) -> list[list[Any]]:
        metadata = self.fields(PUBLICACOES)
        active = [[name, '=', True] for name in ('x_active', 'active')
                  if metadata.get(name, {}).get('type') == 'boolean']
        if not active:
            raise ValueError('Odoo publication archive flag could not be verified')
        return [NEWS_PUBLICATION, *active, [field, '!=' if filled else '=', False]]

    def _avatar_count(self, domain: list[list[Any]]) -> int:
        count = self.call(PUBLICACOES, 'search_count', domain=domain, context=CONTEXT)
        if type(count) is not int or count < 0:
            raise RuntimeError('Invalid Odoo Avatar count')
        return count

    def _avatar_sample(self, field: str, domain: list[list[Any]], *, filled: bool) -> str:
        rows = self.call(
            PUBLICACOES, 'search_read', domain=domain, fields=['id', field], limit=1,
            context={**CONTEXT, 'bin_size': False}, response_limit=AVATAR_RESPONSE_LIMIT,
        )
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or not positive_id(rows[0].get('id')) or field not in rows[0]):
            raise RuntimeError('Odoo Avatar sample was not returned')
        representation, content = _binary(rows[0][field])
        if bool(content) != filled:
            raise RuntimeError('Odoo Avatar sample did not match the binary domain')
        return representation

    def avatar_info(self) -> dict[str, Any]:
        """Inspect the resolved form and report binary shapes without including images."""
        configured = getattr(self.cfg, 'avatar_field', None)
        report: dict[str, Any] = {'model': PUBLICACOES, 'field': None}
        try:
            field, editable = self._avatar_schema()
            report.update(field=field, type='binary', widget='image')
            if configured and configured != field:
                raise ValueError('Configured field does not match the publication form Avatar')
            report['writable'] = editable and self._avatar_access([])
            for label, filled in (('empty', False), ('populated', True)):
                domain = self._avatar_domain(field, filled=filled)
                count = self._avatar_count(domain)
                report[label] = count
                report[f'{label}_representation'] = self._avatar_sample(field, domain, filled=filled) if count else None
        except (RuntimeError, ValueError) as exc:
            return {**report, 'status': 'blocked', 'reason': str(exc)}
        report['status'] = ('confirmed' if configured else 'available') if report['writable'] else 'read_only'
        return report

    def avatar_missing_count(self, field: str) -> int:
        self._require_avatar(field)
        return self._avatar_count(self._avatar_domain(field))

    def avatar_missing_page(self, field: str, *, after_id: int, limit: int) -> list[dict[str, Any]]:
        self._require_avatar(field)
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid Odoo Avatar page bounds')
        rows = self.call(
            PUBLICACOES, 'search_read', domain=[*self._avatar_domain(field), ['id', '>', after_id]],
            fields=['id', *PUBLICATION_IDENTITY, 'x_studio_data_de_publicacao'],
            limit=limit, order='id asc', context=CONTEXT, response_limit=AVATAR_RESPONSE_LIMIT,
        )
        if not isinstance(rows, list) or len(rows) > limit:
            raise RuntimeError('Invalid Odoo Avatar page')
        for row in rows:
            if not isinstance(row, dict) or not positive_id(row.get('id')) or row['id'] <= after_id:
                raise RuntimeError('Invalid Odoo Avatar page order')
            after_id = row['id']
        return rows

    def _avatar_publication(self, publication_id: int, domain: list[list[Any]], fields: list[str],
                            *, binary: bool = False) -> dict[str, Any] | None:
        if not positive_id(publication_id):
            raise ValueError('Invalid Odoo publication ID')
        rows = self.call(
            PUBLICACOES, 'search_read', domain=[['id', '=', publication_id], *domain], fields=['id', *fields],
            limit=2, context={**CONTEXT, 'bin_size': False},
            response_limit=AVATAR_RESPONSE_LIMIT if binary else None,
        )
        if rows == []:
            return None
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or not positive_id(rows[0].get('id')) or rows[0]['id'] != publication_id
                or any(key not in rows[0] for key in fields)):
            raise RuntimeError('Invalid Odoo Avatar publication lookup')
        return rows[0]

    def avatar_missing(self, publication_id: int, field: str) -> bool:
        self._require_avatar(field)
        return self._avatar_publication(publication_id, self._avatar_domain(field), []) is not None

    def confirm_avatar(self, publication_id: int, field: str, encoded: str) -> None:
        self._require_avatar(field)
        _, expected = _binary(encoded)
        row = self._avatar_publication(publication_id, [], [field], binary=True)
        if not row or not expected or _binary(row[field])[1] != expected:
            raise RuntimeError('Odoo Avatar write was not confirmed')

    def write_avatar(self, publication_id: int, field: str, encoded: str, *,
                     expected: dict[str, Any] | None = None) -> bool:
        self._require_avatar(field, write=True)
        if not isinstance(encoded, str) or not _binary(encoded)[1]:
            raise ValueError('Invalid Odoo Avatar write payload')
        if not positive_id(publication_id) or not self._avatar_access([publication_id]):
            raise ValueError('Odoo publication Avatar write access was not verified')
        matches = self._avatar_publication(publication_id, self._avatar_domain(field), list(PUBLICATION_IDENTITY))
        if matches is None:
            return False
        if expected is not None and (expected.get('id') != publication_id or any(
            normalizar_valor(key, matches[key]) != normalizar_valor(key, expected.get(key))
            for key in PUBLICATION_IDENTITY
        )):
            raise RuntimeError('Odoo publication identity changed before the Avatar write')
        try:
            self.update_publicacao(publication_id, {field: encoded})
        except RuntimeError as exc:
            try:
                self.confirm_avatar(publication_id, field, encoded)
            except RuntimeError:
                raise RuntimeError('Odoo Avatar write was not confirmed') from exc
        else:
            self.confirm_avatar(publication_id, field, encoded)
        return True

    # ------------------------------------------------------------------ writes
    def create_publicacao(self, valores: dict[str, Any]) -> int:
        result = self.call(PUBLICACOES, 'create', vals_list=[valores], context=CONTEXT)
        if isinstance(result, list) and len(result) == 1:
            result = result[0]
        if not positive_id(result):
            raise RuntimeError('Unexpected Odoo create response')
        return result

    def update_publicacao(self, publicacao_id: int, valores: dict[str, Any]) -> None:
        result = self.call(
            PUBLICACOES, 'write', ids=[publicacao_id], vals=valores, context=CONTEXT,
        )
        if result is not True:
            raise RuntimeError('Odoo publication update was not confirmed')

    def registrar_sucesso(self, fonte_id: int) -> None:
        """Record a successful collection and clear any previous error."""
        valores = {
            'x_studio_ultima_coleta_bem_sucedida_1':
                datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            'x_studio_ultimo_erro_1': None,
        }
        resultado = self.call(FONTES, 'write', ids=[fonte_id], vals=valores, context=CONTEXT)
        if resultado is not True:
            raise RuntimeError('Odoo source success status was not confirmed')

    def registrar_erro(self, fonte_id: int, erro: str) -> None:
        """Record a collection error without losing the last success timestamp."""
        resultado = self.call(
            FONTES, 'write',
            ids=[fonte_id], vals={'x_studio_ultimo_erro_1': erro}, context=CONTEXT,
        )
        if resultado is not True:
            raise RuntimeError('Odoo source error status was not confirmed')


def campo_publicacao_permitido(nome: str) -> bool:
    return nome in PUBLICACOES_FIELDS and nome not in READONLY_RELATED
