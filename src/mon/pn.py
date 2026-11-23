"""Remote data client."""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Self, cast
import logging
from time import monotonic, sleep
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import ConnectionPool
from urllib3.exceptions import InvalidHeader, MaxRetryError
from urllib3.response import BaseHTTPResponse
from urllib3.util.retry import Retry

from .normalizer import purchase_identity

CONSULTATION_BASE = 'https://pncp.gov.br/api/consulta'
BASE = f'{CONSULTATION_BASE}/v1/contratacoes'

FILES_BASE = 'https://pncp.gov.br/api/pncp'
MAX_DOCUMENTS = 100
MAX_ITEMS = 1000
LOOKUP_PAGE_SIZE = 50
# Local pacing for the sequential collector.
REQUEST_INTERVAL = 3.0
HTTP_RETRIES = 3
RATE_LIMIT_STEP = 30
MAX_RATE_LIMIT_RETRY_AFTER = RATE_LIMIT_STEP * HTTP_RETRIES
MAX_RATE_LIMIT_RETRY_WAIT = MAX_RATE_LIMIT_RETRY_AFTER * HTTP_RETRIES
MAX_RETRY_AFTER = 30


class PNCPRetry(Retry):
    def increment(
        self, method: str | None = None, url: str | None = None,
        response: BaseHTTPResponse | None = None, error: Exception | None = None,
        _pool: ConnectionPool | None = None, _stacktrace: TracebackType | None = None,
    ) -> Self:
        if response is not None:
            limit = MAX_RATE_LIMIT_RETRY_AFTER if response.status == 429 else MAX_RETRY_AFTER
            if (self.get_retry_after(response) or 0) > limit:
                # Return the HTTP response through urllib3's existing exhausted-status path.
                raise MaxRetryError(
                    cast(ConnectionPool, _pool), url,
                    RuntimeError('PNCP Retry-After exceeds the local wait limit'),
                )
        return super().increment(
            method=method, url=url, response=response, error=error,
            _pool=_pool, _stacktrace=_stacktrace,
        )

    def sleep(self, response=None):
        delay = self.get_retry_after(response) if response is not None else None
        if response is not None and response.status == 429:
            if delay is None:
                delay = RATE_LIMIT_STEP * sum(attempt.status == 429 for attempt in self.history)
        else:
            delay = delay or self.get_backoff_time()
        wait = max(REQUEST_INTERVAL, delay)
        if response is not None and response.status == 429:
            logging.getLogger(__name__).warning('PNCP HTTP 429 retry_wait_seconds=%.1f', wait)
        sleep(wait)


class PNCPAdapter(HTTPAdapter):
    def __init__(self):
        self._next_attempt = monotonic()
        super().__init__(max_retries=PNCPRetry(
            total=HTTP_RETRIES, backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods={'GET'}, respect_retry_after_header=True, raise_on_status=False,
        ))

    def finish_attempt(self):
        self._next_attempt = max(self._next_attempt, monotonic() + REQUEST_INTERVAL)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        wait = self._next_attempt - monotonic()
        if wait > 0:
            sleep(wait)
        try:
            response = super().send(
                request, stream=stream, timeout=timeout, verify=verify,
                cert=cert, proxies=proxies,
            )
        finally:
            # Connection setup can delay the actual HTTP request.
            self.finish_attempt()
        if response.is_redirect and 'Retry-After' in response.headers:
            try:
                delay = self.max_retries.get_retry_after(response.raw)
            except InvalidHeader as exc:
                response.close()
                raise requests.exceptions.InvalidHeader('Invalid PNCP redirect wait') from exc
            if delay is None:
                return response
            if delay > MAX_RETRY_AFTER:
                response.close()
                raise RuntimeError('PNCP redirect Retry-After exceeds the local wait limit')
            self._next_attempt = max(self._next_attempt, monotonic() + delay)
        return response


class PNCPSession(requests.Session):
    def __init__(self):
        super().__init__()
        self._pncp_adapter = PNCPAdapter()
        self.mount('https://', self._pncp_adapter)

    def finish_attempt(self):
        self._pncp_adapter.finish_attempt()

    def send(self, request, **kwargs):
        try:
            return super().send(request, **kwargs)
        finally:
            # Requests reads non-streamed response bodies after the adapter returns.
            self.finish_attempt()


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
    kind: str | None = None


class PNCPClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session if session is not None else PNCPSession()
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
        for records in self.list_day_pages(kind, day, modality, page_size, max_pages, uf):
            yield from records

    def list_day_pages(
        self,
        kind: str,
        day,
        modality: int,
        page_size: int,
        max_pages: int,
        uf: str | None = None,
    ):
        """Yield each complete, validated consultation page."""
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
                raise RuntimeError(
                    f'Pagination metadata changed: '
                    f'kind={kind} day={day} modality={modality} '
                    f'page={page} uf={uf or "ALL"} '
                    f'expected_pages={expected_pages} actual_pages={total_pages} '
                    f'expected_records={expected_records} actual_records={total_records}; '
                    'state not advanced'
                )

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
            yield records
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
                documents.append(Document(sequence, title, url, download_path, kind))
        return documents

    def budget_is_public(self, identifier: str) -> bool:
        detail = self._purchase_json(CONSULTATION_BASE + purchase_path(identifier), 'budget lookup')
        if not isinstance(detail, dict) or detail.get('numeroControlePNCP') != identifier:
            raise ValueError('Purchase identity was not confirmed for the budget lookup')
        code = detail.get('orcamentoSigilosoCodigo')
        if type(code) is not int or code not in (1, 2, 3):
            raise ValueError('Missing or invalid purchase confidentiality code')
        return code == 1

    def judgment_criterion(self, identifier: str) -> str | None:
        path = purchase_path(identifier)
        url = f'https://pncp.gov.br/pncp-api{path}/itens'
        seen, criteria = set(), set()
        for page in range(1, MAX_ITEMS // LOOKUP_PAGE_SIZE + 2):
            batch = self._purchase_json(
                url, f'item listing page {page}', pagina=page, tamanhoPagina=LOOKUP_PAGE_SIZE,
            )
            if not isinstance(batch, list) or len(batch) > LOOKUP_PAGE_SIZE:
                raise ValueError('Invalid official item listing')
            if len(seen) + len(batch) > MAX_ITEMS:
                return None
            for item in batch:
                if not isinstance(item, dict):
                    raise ValueError('Invalid official item metadata')
                number = item.get('numeroItem')
                if type(number) is not int or number <= 0 or number in seen:
                    raise ValueError('Invalid or repeated official item number')
                seen.add(number)
                code = item.get('criterioJulgamentoId')
                name = item.get('criterioJulgamentoNome')
                if type(code) is int and code > 0 and isinstance(name, str) and name.strip():
                    criteria.add((code, name.strip()))
                else:
                    criteria.add((None, None))
            if len(batch) < LOOKUP_PAGE_SIZE:
                return criteria.pop()[1] if len(criteria) == 1 else None
        return None
