"""Remote data client."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic, sleep
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InvalidHeader, MaxRetryError
from urllib3.util.retry import Retry

from .normalizer import purchase_identity

CONSULTATION_BASE = 'https://pncp.gov.br/api/consulta'
BASE = f'{CONSULTATION_BASE}/v1/contratacoes'

FILES_BASE = 'https://pncp.gov.br/api/pncp'
MAX_DOCUMENTS = 100
LOOKUP_PAGE_SIZE = 50
# Local pacing for the sequential collector.
REQUEST_INTERVAL = 1.0
MAX_RETRY_AFTER = 30


class PNCPRetry(Retry):
    def __init__(self, pace, **kwargs):
        super().__init__(**kwargs)
        self._pace = pace

    def new(self, **kwargs):
        return super().new(pace=self._pace, **kwargs)

    def increment(self, method=None, url=None, response=None, _pool=None, **kwargs):
        if response is not None and (self.get_retry_after(response) or 0) > MAX_RETRY_AFTER:
            # Return the HTTP response through urllib3's existing exhausted-status path.
            raise MaxRetryError(_pool, url, 'PNCP Retry-After exceeds the local wait limit')
        return super().increment(method, url, response, _pool=_pool, **kwargs)

    def sleep(self, response=None):
        delay = self.get_retry_after(response) if response is not None else None
        self._pace(delay or self.get_backoff_time())


class PNCPAdapter(HTTPAdapter):
    def __init__(self):
        self._next_attempt = None
        super().__init__(max_retries=PNCPRetry(
            self._pace, total=3, backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods={'GET'}, respect_retry_after_header=True, raise_on_status=False,
        ))

    def _pace(self, delay=0):
        now = monotonic()
        gap = self._next_attempt - now if self._next_attempt is not None else 0
        wait = max(delay, gap)
        if wait > 0:
            sleep(wait)
        self._next_attempt = monotonic() + REQUEST_INTERVAL

    def send(self, request, **kwargs):
        self._pace()
        response = super().send(request, **kwargs)
        if response.is_redirect and 'Retry-After' in response.headers:
            try:
                delay = self.max_retries.get_retry_after(response.raw)
            except InvalidHeader as exc:
                response.close()
                raise requests.exceptions.InvalidHeader('Invalid PNCP redirect wait') from exc
            if delay > MAX_RETRY_AFTER:
                response.close()
                raise RuntimeError('PNCP redirect Retry-After exceeds the local wait limit')
            self._next_attempt = max(self._next_attempt, monotonic() + delay)
        return response


def purchase_path(identifier: str) -> str:
    cnpj, year, sequence = purchase_identity(identifier)
    return f'/v1/orgaos/{cnpj}/compras/{year}/{sequence}'


def validate_document_url(url: object, path: str) -> str:
    if (
        not isinstance(url, str) or not url.isascii()
        or any(ord(ch) <= 32 or ord(ch) == 127 or ch in '?#' for ch in url)
    ):
        raise ValueError('Invalid official document URL')
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError('Invalid official document URL') from exc
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
            self.session.mount('https://', PNCPAdapter())
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
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise RuntimeError(f'PNCP consultation page {page}: request failed') from exc

            if resp.status_code == 204:
                if page == 1:
                    return
                raise RuntimeError('Unexpected empty page; state not advanced')
            if resp.status_code != 200:
                raise RuntimeError(
                    f'PNCP consultation page {page}: HTTP {resp.status_code}'
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


    def _purchase_json(self, url: str, operation: str, **params):
        try:
            response = self.session.get(
                url, params=params, timeout=(10, 40), allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f'PNCP {operation}: request failed') from exc
        if response.status_code == 204:
            return []
        if response.status_code != 200:
            raise RuntimeError(f'PNCP {operation}: HTTP {response.status_code}')
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f'PNCP {operation}: invalid response') from exc

    def documents(self, identifier: str) -> list[Document]:
        path = f'{purchase_path(identifier)}/arquivos'
        rows = []
        for page in range(1, MAX_DOCUMENTS // LOOKUP_PAGE_SIZE + 2):
            batch = self._purchase_json(
                FILES_BASE + path, f'document listing page {page}',
                pagina=page, tamanhoPagina=LOOKUP_PAGE_SIZE,
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
        detail = self._purchase_json(CONSULTATION_BASE + purchase_path(identifier), 'budget lookup')
        if not isinstance(detail, dict) or detail.get('numeroControlePNCP') != identifier:
            raise ValueError('Purchase identity was not confirmed for the budget lookup')
        code = detail.get('orcamentoSigilosoCodigo')
        if type(code) is not int or code not in (1, 2, 3):
            raise ValueError('Missing or invalid purchase confidentiality code')
        return code == 1
