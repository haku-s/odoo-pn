"""Odoo JSON-2 client for the data sync models."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import requests

FONTES = 'x_fontes'
ASSUNTOS = 'x_assuntos'
PUBLICACOES = 'x_publicacoes'
MODEL_FIELD = 'ir.model.fields'

CONTEXT = {'lang': 'pt_BR', 'active_test': False}
TEXT = {'char', 'text'}
# Every property consumed by the validation logic must be requested explicitly.
FIELD_ATTRIBUTES = ['type', 'string', 'readonly', 'selection', 'relation', 'related', 'store']
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

OPERATIONS = {
    FONTES: {'fields_get', 'search_read', 'search_count', 'create', 'write', 'has_access'},
    ASSUNTOS: {'fields_get', 'search_read', 'search_count', 'has_access'},
    PUBLICACOES: {'fields_get', 'search_read', 'search_count', 'create', 'write', 'has_access'},
    MODEL_FIELD: {'search_read'},
}

TECHNICAL = re.compile(r'^[a-z][a-z0-9_]*$')
# Operational lookups must ignore archived records.  The instance uses a custom
# boolean field (`x_active`), not the conventional `active` flag, so the filter
# has to be explicit in the domain.
DOMINIO_ATIVOS = [['x_active', '=', True]]


def positive_id(value) -> bool:
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


def _valid(metadata: dict, name: str, types: set[str], *, relation: str | None = None) -> dict:
    meta = metadata.get(name)
    if not isinstance(meta, dict) or meta.get('type') not in types:
        raise ValueError(f'Incompatible Odoo field metadata: {name}')
    if relation and meta.get('relation') != relation:
        raise ValueError(f'Incompatible Odoo relation metadata: {name}')
    return meta


def selection_keys(meta: dict) -> tuple[str, ...]:
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
        self._metadata: dict[str, dict] = {}
        self._selections: dict[str, tuple[str, ...]] = {}

    # ---------------------------------------------------------------- transport
    def call(self, model: str, method: str, **params):
        if method not in OPERATIONS.get(model, ()):
            raise ValueError('Operation not permitted')
        try:
            response = self.session.post(
                f'{self.cfg.odoo_url}/json/2/{model}/{method}',
                headers=self.headers,
                json=params,
                timeout=self.cfg.request_timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f'Odoo {model}/{method}: connection failed') from exc
        if response.status_code != 200:
            raise RuntimeError(f'Odoo {model}/{method}: HTTP {response.status_code}')
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError('Invalid Odoo response format') from exc

    def fields(self, model: str) -> dict:
        if model not in self._metadata:
            result = self.call(
                model, 'fields_get', context=CONTEXT, attributes=FIELD_ATTRIBUTES,
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
                    if set(actual) != set(keys):
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
    def _registros(self, model: str, domain, fields: list[str], limite: int, rotulo: str) -> list[dict]:
        rows = self.call(
            model, 'search_read', domain=domain, fields=['id', *fields],
            limit=limite, context=CONTEXT,
        )
        if not isinstance(rows, list) or len(rows) >= limite:
            raise RuntimeError(f'Incomplete or oversized Odoo listing: {rotulo}')
        return rows

    def fontes(self) -> list[dict]:
        rows = self._registros(
            FONTES, DOMINIO_ATIVOS,
            ['x_name', 'x_studio_codigo_da_fonte_1', 'x_studio_url',
             'x_studio_metodo_de_coleta', 'x_studio_coleta_habilitada'],
            1000, 'sources',
        )
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not positive_id(row.get('id')):
                raise RuntimeError('Invalid Odoo source record')
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

    def assuntos(self) -> list[dict]:
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

    def publicacoes_por_identificador(self, fonte_id: int, identificadores: list[str]) -> dict[str, dict]:
        if not identificadores:
            return {}
        rows = self.call(
            PUBLICACOES, 'search_read',
            domain=[['x_studio_fontes', '=', fonte_id],
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

    # ------------------------------------------------------------------ writes
    def create_publicacao(self, valores: dict) -> int:
        result = self.call(PUBLICACOES, 'create', vals_list=[valores], context=CONTEXT)
        if isinstance(result, list) and len(result) == 1:
            result = result[0]
        if not positive_id(result):
            raise RuntimeError('Unexpected Odoo create response')
        return result

    def update_publicacao(self, publicacao_id: int, valores: dict) -> None:
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
