"""Compare direct HTTPS clients on the approved government news routes."""

from concurrent.futures import ThreadPoolExecutor
import json
from time import monotonic
from urllib.parse import urlsplit

import requests

from .network import FetchError, read_public_response
from .sources import DIAGNOSTIC_CHANNELS
from .news_html import HEALTH_LISTING

TIMEOUT = (5, 8)
MAX_BYTES = 5 * 1024 * 1024


def _compare(entry: tuple[str, str]) -> tuple[str, dict[str, dict[str, int | str | None]]]:
    code, url = entry
    results: dict[str, dict[str, int | str | None]] = {}
    for mode in ('production', 'conventional'):
        started = monotonic()
        result: dict[str, int | str | None]
        try:
            if mode == 'production':
                body, _, status = read_public_response(url, allowed_urls={url}, max_bytes=MAX_BYTES,
                                                       timeout=TIMEOUT, accepted_statuses=tuple(range(200, 600)))
                result = {'http': status, 'bytes': len(body)}
            else:
                with requests.Session() as session:
                    session.trust_env = False
                    with session.get(
                        url, stream=True, allow_redirects=False, timeout=TIMEOUT,
                        headers={'User-Agent': 'odoo-data-sync/1.0', 'Accept-Encoding': 'identity', 'Accept': '*/*'},
                    ) as response:
                        result = {'http': response.status_code, 'encoding': response.headers.get('Content-Encoding')}
                        if response.status_code == 200:
                            length = 0
                            for chunk in response.iter_content(65536):
                                length += len(chunk)
                                if length > MAX_BYTES or monotonic() - started > sum(TIMEOUT):
                                    raise RuntimeError('Body limit reached')
                            result['bytes'] = length
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            result = {'error': str(exc) if isinstance(exc, FetchError) else type(exc).__name__}
        results[mode] = {**result, 'milliseconds': round((monotonic() - started) * 1000)}
    return code, results


def main() -> None:
    endpoints = [(code, HEALTH_LISTING.url if code == 'ministerio_saude' else url)
                 for code, url in DIAGNOSTIC_CHANNELS.items() if urlsplit(url).hostname == 'www.gov.br']
    with ThreadPoolExecutor(max_workers=4) as executor:
        print(json.dumps({'government_transport_probe': dict(executor.map(_compare, endpoints))}, sort_keys=True))


if __name__ == '__main__':
    main()
