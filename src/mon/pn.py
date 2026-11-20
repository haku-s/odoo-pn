
"""Remote data client."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = 'https://pncp.gov.br/api/consulta/v1/contratacoes'


class PNCPClient:
    def __init__(
        self,
        session: requests.Session | None = None,
    ):
        self.session = session or requests.Session()

        if session is None:
            retry = Retry(
                total=3,
                backoff_factor=1.0,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods={'GET'},
                respect_retry_after_header=True,
            )

            self.session.mount(
                'https://',
                HTTPAdapter(max_retries=retry),
            )

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

        while True:
            params['pagina'] = page

            try:
                resp = self.session.get(
                    f'{BASE}/{kind}',
                    params=params,
                    timeout=(10, 40),
                )
            except requests.RequestException as exc:
                raise RuntimeError(
                    'Remote service connection failed'
                ) from exc

            if resp.status_code == 204:
                return

            if not resp.ok:
                raise RuntimeError(
                    f'Remote request failed: HTTP {resp.status_code}'
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise RuntimeError(
                    'Invalid response format'
                ) from exc

            if (
                not isinstance(data, dict)
                or not isinstance(data.get('data'), list)
            ):
                raise RuntimeError(
                    'Unexpected response structure'
                )

            records = data['data']
            total_pages = data.get('totalPaginas')

            if (
                type(total_pages) is not int
                or total_pages < 0
            ):
                raise RuntimeError(
                    'Invalid pagination metadata'
                )

            if total_pages > max_pages:
                raise RuntimeError(
                    'Page limit exceeded; state not advanced'
                )

            for record in records:
                if not isinstance(record, dict):
                    raise RuntimeError(
                        'Unexpected record format'
                    )

                yield record

            if page >= total_pages:
                return

            page += 1
