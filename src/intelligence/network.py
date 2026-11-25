"""Bounded HTTPS reads from explicitly approved public destinations."""

from ipaddress import ip_address
import socket
import ssl
from time import monotonic
from urllib.parse import urljoin, urlsplit

from requests.certs import where
from urllib3 import HTTPSConnectionPool, Timeout
from urllib3.exceptions import ConnectTimeoutError, HTTPError, ProtocolError, ReadTimeoutError

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """A public request failure with safe operational details."""

    def __init__(self, message: str, *, retry: bool = False, delay: float = 1):
        super().__init__(message)
        self.retry = retry
        self.delay = delay


def status_error(status: int, retry_after: str | None) -> FetchError:
    # Defer to the next run for long or unrecognized server-requested waits.
    wait = retry_after.strip() if retry_after else ''
    delay = int(wait) if wait.isascii() and wait.isdecimal() and len(wait) < 4 else 60 if wait else 1
    return FetchError(f'Public HTTPS request returned HTTP {status}',
                      retry=status in TRANSIENT_STATUS and delay <= 5, delay=max(1, delay))


def validate_url(url: str, allowed_hosts: set[str] | frozenset[str]) -> None:
    if not isinstance(url, str) or len(url) > 4096 or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 or char == '\\' for char in url
    ):
        raise ValueError('Invalid public HTTPS URL')
    parts = urlsplit(url)
    host = parts.hostname
    if (
        parts.scheme != 'https' or not host or host not in allowed_hosts
        or parts.netloc.lower() not in (host, f'{host}:443') or '#' in url
    ):
        raise ValueError('Public HTTPS destination is not approved')


def _public_addresses(host: str) -> list[str]:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError('Public HTTPS DNS lookup failed', retry=exc.errno == socket.EAI_AGAIN) from exc
    addresses = []
    for *_, address in answers:
        ip = ip_address(str(address[0]))
        if not ip.is_global or ip.is_multicast or ip.is_reserved or getattr(ip, 'is_site_local', False):
            raise ValueError('Public HTTPS destination resolved to a non-public address')
        addresses.append(str(ip))
    if not addresses:
        raise RuntimeError('Public HTTPS DNS lookup returned no addresses')
    # Connect directly to the validated address while verifying the original hostname.
    return list(dict.fromkeys(addresses))


def _open_https(hostname: str, target: str, deadline: float, timeout: tuple[int, int]):
    failures = []
    for address in _public_addresses(hostname)[:4]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        pool = HTTPSConnectionPool(
            address, port=443, server_hostname=hostname, assert_hostname=hostname,
            cert_reqs='CERT_REQUIRED', ca_certs=where(),
        )
        try:
            response = pool.urlopen(
                'GET', target,
                headers={'Host': hostname, 'Accept-Encoding': 'identity',
                         'User-Agent': 'odoo-data-sync/1.0'},
                timeout=Timeout(total=remaining, connect=timeout[0], read=timeout[1]),
                preload_content=False, decode_content=False, retries=False, redirect=False,
            )
        except (OSError, HTTPError) as exc:
            pool.close()
            if isinstance(exc, ssl.SSLError) or not isinstance(exc, (OSError, ConnectTimeoutError, ReadTimeoutError)):
                raise
            family = ip_address(address).version
            cause = exc.__cause__ or exc
            errno = getattr(cause, 'errno', None)
            failures.append(f'IPv{family}:{type(exc).__name__}' + (f'({errno})' if type(errno) is int else ''))
        else:
            return pool, response
    if failures:
        raise FetchError(f'Public HTTPS connection failed ({", ".join(failures)})', retry=True)
    raise FetchError('Public HTTPS request timed out before response', retry=True)


def read_public(
    url: str, *, allowed_urls: set[str] | frozenset[str] | None = None,
    allowed_hosts: set[str] | frozenset[str] | None = None, max_bytes: int,
    timeout: tuple[int, int],
) -> tuple[bytes, str]:
    body, final, _ = read_public_response(
        url, allowed_urls=allowed_urls, allowed_hosts=allowed_hosts, max_bytes=max_bytes, timeout=timeout,
    )
    return body, final


def read_public_response(
    url: str, *, allowed_urls: set[str] | frozenset[str] | None = None,
    allowed_hosts: set[str] | frozenset[str] | None = None, max_bytes: int,
    timeout: tuple[int, int], accepted_statuses: tuple[int, ...] = (200,),
) -> tuple[bytes, str, int]:
    if allowed_hosts is None:
        allowed_hosts = {urlsplit(value).hostname or '' for value in allowed_urls or ()}
    deadline = monotonic() + sum(timeout)
    for _ in range(4):
        validate_url(url, allowed_hosts)
        if allowed_urls is not None and url not in allowed_urls:
            raise ValueError('Public HTTPS URL is not approved')
        parts = urlsplit(url)
        hostname = parts.hostname
        assert hostname is not None
        target = parts.path or '/'
        if parts.query:
            target += '?' + parts.query
        try:
            pool, response = _open_https(hostname, target, deadline, timeout)
            try:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get('Location')
                    if not location:
                        raise RuntimeError('Public HTTPS redirect has no destination')
                    url = urljoin(url, location)
                    continue
                if response.status not in accepted_statuses:
                    raise status_error(response.status, response.headers.get('Retry-After'))
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise RuntimeError('Unsupported public HTTPS content encoding')
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isdecimal() or int(length) > max_bytes):
                    raise RuntimeError('Public HTTPS response exceeds its size limit')
                data = bytearray()
                while monotonic() < deadline:
                    chunk = response.read1(min(65536, max_bytes - len(data) + 1), decode_content=False)
                    if not chunk:
                        if length is not None and len(data) != int(length):
                            raise FetchError('Public HTTPS response ended before its declared length', retry=True)
                        return bytes(data), url, response.status
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise RuntimeError('Public HTTPS response exceeds its size limit')
                raise FetchError('Public HTTPS response timed out', retry=True)
            finally:
                response.close()
                pool.close()
        except (OSError, HTTPError) as exc:
            retry = not isinstance(exc, ssl.SSLError) and isinstance(exc, (OSError, ReadTimeoutError, ProtocolError))
            raise FetchError(f'Public HTTPS response failed ({type(exc).__name__})', retry=retry) from exc
    raise RuntimeError('Public HTTPS redirect limit reached')
