"""Bounded HTTPS reads from explicitly approved public destinations."""

from ipaddress import ip_address
import socket
from time import monotonic
from urllib.parse import urljoin, urlsplit

from requests.certs import where
from urllib3 import HTTPSConnectionPool, Timeout
from urllib3.exceptions import HTTPError


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


def _public_address(host: str) -> str:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RuntimeError('Public HTTPS DNS lookup failed') from exc
    addresses = []
    for *_, address in answers:
        ip = ip_address(str(address[0]))
        if not ip.is_global or ip.is_multicast or ip.is_reserved or getattr(ip, 'is_site_local', False):
            raise ValueError('Public HTTPS destination resolved to a non-public address')
        addresses.append(str(ip))
    if not addresses:
        raise RuntimeError('Public HTTPS DNS lookup returned no addresses')
    # Connect directly to the validated address while verifying the original hostname.
    return addresses[0]


def read_public(
    url: str, *, allowed_urls: set[str] | frozenset[str] | None = None,
    allowed_hosts: set[str] | frozenset[str] | None = None, max_bytes: int,
    timeout: tuple[int, int],
) -> tuple[bytes, str]:
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
        address = _public_address(hostname)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError('Public HTTPS request timed out')
        pool = HTTPSConnectionPool(
            address, port=443, server_hostname=hostname, assert_hostname=hostname,
            cert_reqs='CERT_REQUIRED', ca_certs=where(),
        )
        target = parts.path or '/'
        if parts.query:
            target += '?' + parts.query
        try:
            response = pool.urlopen(
                'GET', target,
                headers={'Host': hostname, 'Accept-Encoding': 'identity',
                         'User-Agent': 'odoo-data-sync/1.0'},
                timeout=Timeout(total=remaining, connect=timeout[0], read=timeout[1]),
                preload_content=False, decode_content=False, retries=False, redirect=False,
            )
            try:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get('Location')
                    if not location:
                        raise RuntimeError('Public HTTPS redirect has no destination')
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise RuntimeError(f'Public HTTPS request returned HTTP {response.status}')
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise RuntimeError('Unsupported public HTTPS content encoding')
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isdecimal() or int(length) > max_bytes):
                    raise RuntimeError('Public HTTPS response exceeds its size limit')
                data = bytearray()
                while monotonic() < deadline:
                    chunk = response.read1(min(65536, max_bytes - len(data) + 1), decode_content=False)
                    if not chunk:
                        return bytes(data), url
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise RuntimeError('Public HTTPS response exceeds its size limit')
                raise RuntimeError('Public HTTPS request timed out')
            finally:
                response.close()
        except (OSError, HTTPError) as exc:
            raise RuntimeError('Public HTTPS request failed') from exc
        finally:
            pool.close()
    raise RuntimeError('Public HTTPS redirect limit reached')
