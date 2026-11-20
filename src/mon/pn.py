"""Remote data client."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .normalizer import purchase_identity

CONSULTATION_BASE = 'https://pncp.gov.br/api/consulta'
BASE = f'{CONSULTATION_BASE}/v1/contratacoes'

FILES_BASE = 'https://pncp.gov.br/api/pncp'
MAX_DOCUMENTS = 100
LOOKUP_PAGE_SIZE = 50


def purchase_path(identifier: str) -> str:
    cnpj, year, sequence = purchase_identity(identifier)
    return f'/v1/orgaos/{cnpj}/compras/{year}/{sequence}'


def validate_document_url(url: object, path: str) -> str:
    if (
        not isinstance(url, str) or not url.isascii()
        or any(ord(ch) <= 32 or ord(ch) == 127 or ch in '?#' for ch in url)
    ):
        raise ValueError('Invalid official document URL')
    parsed = urlsplit(url)
    if (
        parsed.scheme != 'https' or parsed.netloc not in ('pncp.gov.br', 'pncp.gov.br:443')
        or parsed.query or parsed.fragment
        or parsed.path not in (f'/pncp-api{path}', f'/api/pncp{path}')
    ):
        raise ValueError('Document URL does not match the official purchase and document')
    return url


@dataclass(frozen=True)
class Document:
    sequence: int
    title: str
    url: str
    path: str


class PNCPClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session if session is not None else requests.Session()
        if session is None:
            retry = Retry(
                total=3,
                backoff_factor=1.0,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods={'GET'},
                respect_retry_after_header=True,
                raise_on_status=False,
            )
            self.session.mount('https://', HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            'Accept': 'application/json',
            'User-Agent': 'pnx/1.0',
        })

    def list_day(
        self,
        kind: str,
        day,
        modality: int,
        page_size: int,
        max_pages: int,
        uf: str | None = None,
    ):
        if kind not in ('publicacao', 'atualizacao'):
            raise ValueError('Unsupported operation')
        params = {
            'dataInicial': day.strftime('%Y%m%d'),
            'dataFinal': day.strftime('%Y%m%d'),
            'codigoModalidadeContratacao': modality,
            'tamanhoPagina': page_size,
        }
        if uf:
            params['uf'] = uf

        page = 1
        expected_pages = expected_records = None
        received = 0
        identifiers = set()
        while True:
            params['pagina'] = page
            try:
                resp = self.session.get(
                    f'{BASE}/{kind}',
                    params=params,
                    timeout=(10, 40),
                )
            except requests.RequestException as exc:
                raise RuntimeError('Remote service connection failed') from exc

            if resp.status_code == 204:
                if page == 1:
                    return
                raise RuntimeError('Unexpected empty page; state not advanced')
            if resp.status_code != 200:
                raise RuntimeError(
                    f'Remote request failed: HTTP {resp.status_code}'
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise RuntimeError('Invalid response format') from exc
            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                raise RuntimeError('Unexpected response structure')

            records = data['data']
            total_pages = data.get('totalPaginas')
            total_records = data.get('totalRegistros')
            if (
                ('totalRegistros' in data and (
                    type(total_records) is not int or total_records < 0
                ))
                or ('numeroPagina' in data and (
                    type(data['numeroPagina']) is not int or data['numeroPagina'] != page
                ))
                or len(records) > page_size
            ):
                raise RuntimeError('Invalid page size or pagination metadata')
            if page == 1 and type(total_pages) is int and total_pages == 0 and not records:
                if total_records in (None, 0):
                    return
                raise RuntimeError('Missing records in empty PNCP result')
            if type(total_pages) is not int or total_pages < page:
                raise RuntimeError('Invalid pagination metadata')
            if total_pages > max_pages:
                raise RuntimeError('Page limit exceeded; state not advanced')
            if expected_pages is None:
                expected_pages, expected_records = total_pages, total_records
            elif (total_pages, total_records) != (expected_pages, expected_records):
                raise RuntimeError('Pagination metadata changed; state not advanced')
            received += len(records)
            if expected_records is not None and (
                received > expected_records or (page == total_pages and received != expected_records)
            ):
                raise RuntimeError('Incomplete PNCP record count')
            if not records:
                raise RuntimeError('Unexpected empty page; state not advanced')

            for record in records:
                if not isinstance(record, dict):
                    raise RuntimeError('Unexpected record format')
                identifier = record.get('numeroControlePNCP')
                purchase_identity(identifier)
                if identifier in identifiers:
                    raise RuntimeError('Repeated record in PNCP pagination')
                identifiers.add(identifier)
                yield record
            if page >= total_pages:
                return
            page += 1


    def _purchase_json(self, url: str, **params):
        try:
            response = self.session.get(
                url, params=params, timeout=(10, 40), allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError('PNCP purchase lookup failed') from exc
        if response.status_code == 204:
            return []
        if response.status_code != 200:
            raise RuntimeError(f'PNCP purchase lookup failed: HTTP {response.status_code}')
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError('Invalid PNCP purchase response') from exc

    def documents(self, identifier: str) -> list[Document]:
        path = f'{purchase_path(identifier)}/arquivos'
        rows = []
        for page in range(1, MAX_DOCUMENTS // LOOKUP_PAGE_SIZE + 2):
            batch = self._purchase_json(
                FILES_BASE + path, pagina=page, tamanhoPagina=LOOKUP_PAGE_SIZE,
            )
            if (
                not isinstance(batch, list) or len(batch) > LOOKUP_PAGE_SIZE
                or len(rows) + len(batch) > MAX_DOCUMENTS
            ):
                raise ValueError('Invalid or oversized official document listing')
            rows.extend(batch)
            if len(batch) < LOOKUP_PAGE_SIZE:
                break
        documents, seen = [], set()
        cnpj, year, purchase_sequence = purchase_identity(identifier)
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid document metadata')
            sequence = row.get('sequencialDocumento')
            title = row.get('titulo')
            kind = row.get('tipoDocumentoNome')
            active = row.get('statusAtivo', True)
            if (
                type(sequence) is not int or sequence <= 0 or sequence in seen
                or not isinstance(title, str) or not title.strip() or len(title) > 1024
                or not isinstance(kind, str) or not kind.strip()
                or type(active) is not bool
                or any(key in row and (
                    type(row[key]) is not type(value) or row[key] != value
                ) for key, value in (('cnpj', cnpj), ('anoCompra', year),
                                     ('sequencialCompra', purchase_sequence)))
            ):
                raise ValueError('Invalid document metadata')
            seen.add(sequence)
            if active:
                download_path = f'{path}/{sequence}'
                url = validate_document_url(row.get('url'), download_path)
                documents.append(Document(sequence, title, url, download_path))
        return documents

    def budget_is_public(self, identifier: str) -> bool:
        detail = self._purchase_json(CONSULTATION_BASE + purchase_path(identifier))
        if not isinstance(detail, dict) or detail.get('numeroControlePNCP') != identifier:
            raise ValueError('Purchase identity was not confirmed for the budget lookup')
        code = detail.get('orcamentoSigilosoCodigo')
        if type(code) is not int or code not in (1, 2, 3):
            raise ValueError('Missing or invalid purchase confidentiality code')
        return code == 1
