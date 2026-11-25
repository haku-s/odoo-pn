"""Persist scientific observations in their own category of Odoo publications."""

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

from .odoo import FONTES, PUBLICACOES, SCIENCE_TYPE, OdooClient
from .science import Row, _keys, reconcile

SOURCE_CODE = 'science_metadata'
IDENTIFIERS = {'x_science_doi': 'doi', 'x_science_pmid': 'pmid'}
NATIVE = {'x_science_scielo': 'scielo:', 'x_science_openalex': 'openalex:', 'x_science_inpi': 'inpi-radar:'}
SCIENCE_FIELDS = {
    'x_science_doi': 'char', 'x_science_pmid': 'char', 'x_science_scielo': 'char',
    'x_science_openalex': 'char', 'x_science_inpi': 'char', 'x_science_published': 'char',
    'x_science_indexed': 'date', 'x_science_kind': 'char', 'x_science_providers': 'char',
    'x_science_links': 'text', 'x_science_observations': 'text',
}
HEAD_FIELDS = list(SCIENCE_FIELDS) + ['x_name', 'x_active', 'x_studio_url', 'x_studio_identificador_externo']
MAX_OBSERVATION_BYTES = 512 * 1024


def scientific_values(providers: dict[str, Row]) -> dict[str, Any]:
    ordered = sorted(providers, key=lambda name: (
        ('pubmed', 'scielo_articlemeta', 'crossref', 'scielo_crossref', 'openalex', 'inpi_radars').index(name)
    ))
    rows = [providers[name] for name in ordered]
    dates = [row.get('dates', {}) for row in rows]
    published = next((values['published'] for values in dates if values.get('published')), False)
    indexed = next((values[key] for key in ('pubmed_created', 'indexed', 'pubmed_entry') for values in dates
                    if values.get(key)), None)
    # The provider timestamp remains intact in the scientific observations.
    indexed_day = date.fromisoformat(indexed[:10]).isoformat() if indexed else False
    official_ids = {row['official_id'] for row in rows if row.get('official_id')}
    evidence = json.dumps(providers, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(evidence.encode()) > MAX_OBSERVATION_BYTES:
        raise ValueError('Scientific observations exceed the storage limit')
    return {
        'x_name': next(row['title'] for row in rows if row.get('title')),
        'x_studio_url': next(row['url'] for row in rows if row.get('url')),
        **{field: next((row[key] for row in rows if row.get(key)), False) for field, key in IDENTIFIERS.items()},
        **{field: next((key for key in sorted(official_ids) if key.startswith(prefix)), False)
           for field, prefix in NATIVE.items()},
        'x_science_published': published, 'x_science_indexed': indexed_day,
        'x_science_kind': next((row['kind'] for row in rows if row.get('kind') not in (None, 'unknown')), 'unknown'),
        'x_science_providers': ', '.join(ordered),
        'x_science_links': json.dumps({name: {key: providers[name][key] for key in ('notices', 'related_works')
                                            if providers[name].get(key)} for name in ordered
                                       if providers[name].get('notices') or providers[name].get('related_works')},
                                      sort_keys=True, ensure_ascii=False),
        'x_science_observations': evidence,
    }


class ScienceStore:
    def __init__(self, client: OdooClient):
        self.client = client
        self.source_id: int | None = None

    def check(self, *, write: bool = False) -> None:
        self.client.check_fields(PUBLICACOES, SCIENCE_FIELDS | {
            'x_name': 'char', 'x_active': 'boolean', 'x_studio_url': 'char', 'x_studio_tipo_de_publicacao': 'selection',
            'x_studio_fontes': 'many2one', 'x_studio_primeira_coleta': 'datetime',
            'x_studio_ultima_atualizacao': 'datetime',
        }, relations={'x_studio_fontes': FONTES}, write=write, access=('read', 'create', 'write') if write else ('read',))
        fields = self.client.fields(PUBLICACOES)
        key = 'x_studio_identificador_externo'
        kind = fields.get(key, {}).get('type')
        if kind not in ('char', 'text'):
            raise ValueError('Incompatible scientific external identifier field')
        self.client.check_fields(PUBLICACOES, {key: kind}, write=write, access=())
        if SCIENCE_TYPE not in self.client.selection(PUBLICACOES, 'x_studio_tipo_de_publicacao'):
            raise ValueError('Scientific publication selection is missing')
        if write:
            for key in ('x_studio_assunto_1', 'x_studio_data_de_publicacao', 'x_studio_resumo'):
                if fields.get(key, {}).get('required') is not False:
                    raise ValueError(f'Science cannot safely fill the required publication field: {key}')
            if self.client.call(PUBLICACOES, 'has_access', ids=[], operation='unlink') is not False:
                raise ValueError('Scientific integration must not have publication deletion access')
        sources = self.client.storage_rows(FONTES, [['x_studio_codigo_da_fonte_1', '=', SOURCE_CODE]],
                                           ['x_active', 'x_studio_coleta_habilitada'], 2)
        if len(sources) != 1 or sources[0].get('x_active') is not True or sources[0].get('x_studio_coleta_habilitada') is not False:
            raise ValueError('Scientific publication source must exist, be active and stay disabled for news collection')
        self.source_id = sources[0]['id']

    def _find(self, values: dict[str, Any]) -> list[Row]:
        keys = [[field, '=', values[field]] for field in (*IDENTIFIERS, *NATIVE) if values[field]]
        if not keys:
            raise ValueError('Missing scientific identity')
        return self.client.storage_rows(PUBLICACOES, [
            ['x_studio_tipo_de_publicacao', '=', SCIENCE_TYPE],
            ['x_studio_fontes', '=', self.source_id], *(['|'] * (len(keys) - 1)), *keys,
        ], HEAD_FIELDS, 2)

    def put(self, group: Row) -> str:
        observations = group['observations']
        incoming = scientific_values(observations)
        rows = self._find(incoming)
        if len(rows) > 1:
            raise ValueError('Scientific identities require manual reconciliation')
        current = rows[0] if rows else {}
        previous = []
        if current:
            previous = [{'observations': json.loads(current['x_science_observations'])}]
        groups = reconcile(previous, list(observations.items()))
        if len(groups) != 1:
            raise ValueError('Scientific evidence conflicts with an existing identity')
        desired = scientific_values(groups[0]['observations'])
        changes = {key: value for key, value in desired.items() if current.get(key) != value}
        if not changes:
            return 'unchanged'
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        changes['x_studio_ultima_atualizacao'] = now
        if not current:
            keys = sorted(set().union(*(_keys(row) for row in observations.values())))
            identifier = 'science:' + hashlib.sha256(json.dumps(keys).encode()).hexdigest()
            changes.update(x_studio_tipo_de_publicacao=SCIENCE_TYPE, x_studio_fontes=self.source_id,
                           x_studio_identificador_externo=identifier, x_studio_primeira_coleta=now, x_active=True)
        else:
            identifier = current['x_studio_identificador_externo']
        self.client.storage_write(PUBLICACOES, current.get('id'), changes)
        confirmed = self._find(desired)
        if (len(confirmed) != 1 or (current and confirmed[0]['id'] != current['id'])
                or (not current and confirmed[0].get('x_active') is not True)
                or confirmed[0].get('x_studio_identificador_externo') != identifier
                or any(confirmed[0].get(key) != value for key, value in desired.items())):
            raise RuntimeError('Scientific publication write could not be confirmed')
        return 'updated' if current else 'created'
