"""Validate and reconcile official documents without replacing existing files."""

from __future__ import annotations

import base64
import hashlib
import io
import re
import zipfile
from pathlib import PurePosixPath
from time import monotonic
from urllib.parse import urljoin

import requests

from .odoo import ATTACHMENT, TASK, direct_attachment
from .pn import MAX_RATE_LIMIT_RETRY_WAIT, Document, PNCPSession, validate_document_url

MAX_BYTES = 40 * 1024 * 1024
DOWNLOAD_SECONDS = 120
# shortcut: additional formats need verified signatures before upload support.
MIME = {
    '.pdf': ('pdf', 'application/pdf'),
    '.zip': ('zip', 'application/zip'),
    '.docx': ('zip', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    '.xlsx': ('zip', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    '.pptx': ('zip', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
    '.odt': ('zip', 'application/vnd.oasis.opendocument.text'),
    '.ods': ('zip', 'application/vnd.oasis.opendocument.spreadsheet'),
    '.sxw': ('zip', 'application/vnd.sun.xml.writer'),
    '.doc': ('ole', 'application/msword'),
    '.xls': ('ole', 'application/vnd.ms-excel'),
    '.ppt': ('ole', 'application/vnd.ms-powerpoint'),
    '.rar': ('rar', 'application/vnd.rar'),
    '.7z': ('7z', 'application/x-7z-compressed'),
    '.rtf': ('rtf', 'application/rtf'),
    '.txt': ('txt', 'text/plain'),
}
DEFAULT_EXT = {
    'pdf': '.pdf', 'zip': '.zip', 'ole': '.doc',
    'rar': '.rar', '7z': '.7z', 'rtf': '.rtf', 'txt': '.txt',
}
ZIP_MEMBERS = {
    '.docx': 'word/document.xml', '.xlsx': 'xl/workbook.xml',
    '.pptx': 'ppt/presentation.xml', '.odt': 'content.xml',
    '.ods': 'content.xml', '.sxw': 'content.xml',
}
MARKER = 'PNCP document: '


def safe_name(document: Document) -> str:
    title = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]+', '_', document.title)
    title = re.sub(r'\s+', ' ', title).strip(' .') or f'PNCP document {document.sequence}'
    ext = PurePosixPath(title).suffix.lower()
    if len(title) > 160 and ext in MIME:
        return title[:160 - len(ext)].rstrip(' .') + ext
    return title[:160].rstrip(' .')


def _binary_kind(data: bytes) -> str:
    if data.startswith(b'%PDF-') and data.rstrip().endswith(b'%%EOF'):
        return 'pdf'
    if data.startswith(b'PK\x03\x04') and zipfile.is_zipfile(io.BytesIO(data)):
        return 'zip'
    for signature, minimum, kind in (
        (bytes.fromhex('d0cf11e0a1b11ae1'), 512, 'ole'),
        (b'Rar!\x1a\x07', 20, 'rar'),
        (b'7z\xbc\xaf\x27\x1c', 32, '7z'),
        (b'{\\rtf', 8, 'rtf'),
    ):
        if len(data) >= minimum and data.startswith(signature):
            return kind
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = ''
    if (
        text.strip() and not text.lstrip().startswith(('<', '{', '[', '%PDF-', 'PK', 'Rar!'))
        and all(ord(ch) >= 32 or ch in '\r\n\t' for ch in text)
    ):
        return 'txt'
    raise ValueError('Empty, malformed, or unsupported official document')


def name_and_mime(document: Document, data: bytes) -> tuple[str, str]:
    kind = _binary_kind(data)
    name = safe_name(document)
    ext = PurePosixPath(name).suffix.lower()
    if ext not in MIME:
        ext = DEFAULT_EXT[kind]
        name += ext
    expected, content_type = MIME[ext]
    if expected != kind:
        raise ValueError('Official document extension does not match its content')
    if kind == 'zip':
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                member = ZIP_MEMBERS.get(ext)
                if member:
                    archive.getinfo(member)
        except (KeyError, zipfile.BadZipFile) as exc:
            raise ValueError('Invalid official document archive') from exc
    return name, content_type


def download(session: requests.Session, document: Document) -> bytes:
    url = document.url
    deadline = monotonic() + DOWNLOAD_SECONDS + MAX_RATE_LIMIT_RETRY_WAIT
    try:
        for _ in range(4):
            if monotonic() >= deadline:
                raise RuntimeError('Official document download exceeded its time limit')
            validate_document_url(url, document.path)
            with session.get(
                url, stream=True, allow_redirects=False, timeout=(10, 40),
                headers={'Accept': '*/*', 'Accept-Encoding': 'identity'},
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location')
                    if not location:
                        raise ValueError('Official document redirect has no destination')
                    try:
                        url = urljoin(url, location)
                    except ValueError as exc:
                        raise ValueError('Invalid official document redirect URL') from exc
                    continue
                if response.status_code != 200:
                    raise RuntimeError(f'Official document download: HTTP {response.status_code}')
                # Retry waits can precede the body, which still has its own time limit.
                deadline = min(deadline, monotonic() + DOWNLOAD_SECONDS)
                headers = response.headers
                kind = headers.get('Content-Type', '').split(';')[0].strip().lower()
                if (
                    kind in ('text/html', 'application/xhtml+xml', 'text/xml', 'application/xml',
                             'application/json')
                    or kind.endswith(('+json', '+xml'))
                    or headers.get('Content-Encoding', 'identity').lower() != 'identity'
                ):
                    raise ValueError('Unexpected official document response type')
                length = headers.get('Content-Length')
                if length is not None and (
                    not re.fullmatch(r'[0-9]{1,12}', length) or not 0 < int(length) <= MAX_BYTES
                ):
                    raise ValueError('Invalid or excessive official document length')
                content = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if monotonic() > deadline:
                        raise RuntimeError('Official document download exceeded its time limit')
                    if len(content) + len(chunk) > MAX_BYTES:
                        raise ValueError('Official document exceeds its size limit')
                    content.extend(chunk)
                if not content or (length is not None and len(content) != int(length)):
                    raise RuntimeError('Incomplete official document download')
                return bytes(content)
    except requests.RequestException as exc:
        raise RuntimeError('Official document download interrupted') from exc
    finally:
        if isinstance(session, PNCPSession):
            session.finish_attempt()
    raise RuntimeError('Official document redirect limit exceeded')


def source_marker(document: Document) -> str:
    return f'{MARKER}{document.path}'


def pending_documents(documents: list[Document], attachments: list[dict]) -> tuple[int, int]:
    pending = 0
    for doc in documents:
        name = safe_name(doc)
        candidates = {name} | {name + ext for ext in MIME}
        if not any(
            row.get('description') == source_marker(doc) or row.get('name') in candidates
            for row in attachments
        ):
            pending += 1
    return pending, len(documents) - pending


def attach(odoo, session, identifier: str, task_id: int, document: Document,
           existing: list[dict]) -> bool:
    content = download(session, document)
    name, mime = name_and_mime(document, content)
    checksum = hashlib.sha1(content, usedforsecurity=False).hexdigest()
    marker = source_marker(document)

    if any(
        row.get('description') == marker and row.get('checksum') != checksum
        for row in existing
    ):
        raise RuntimeError('Official document content changed under an existing identity')
    same_name = [row for row in existing if row.get('name') == name and row.get('checksum') != checksum]
    if same_name:
        root = f"{MARKER}{document.path.rsplit('/', 1)[0]}/"
        if not all(
            re.fullmatch(re.escape(root) + r'[1-9][0-9]*', str(row.get('description')))
            and row.get('description') != marker for row in same_name
        ):
            raise RuntimeError('Attachment name already exists with a different checksum')
        ext = PurePosixPath(name).suffix
        name = f'{name[:-len(ext)]} (PNCP {document.sequence}){ext}'
        if any(row.get('name') == name and row.get('checksum') != checksum for row in existing):
            raise RuntimeError('Revision filename already exists with a different checksum')
    if any(row.get('checksum') == checksum for row in existing):
        return False

    task = odoo.tasks([identifier]).get(identifier)
    if task is None or task['id'] != task_id:
        raise RuntimeError('Task identity changed before attachment upload')
    values = {
        'name': name, 'description': marker, 'type': 'binary', 'mimetype': mime,
        'raw': base64.b64encode(content).decode('ascii'), 'res_model': TASK, 'res_id': task_id,
    }

    def confirmed(rows, attachment_id=None):
        matches = [
            row for row in rows if direct_attachment(row, task_id)
            and row.get('name') == name and row.get('description') == marker
            and row.get('checksum') == checksum
        ]
        return (
            len(matches) == 1 and (attachment_id is None or matches[0]['id'] == attachment_id)
        )

    try:
        attachment_id = odoo.create(ATTACHMENT, values)
    except RuntimeError as exc:
        rows = odoo.attachments(task_id)
        if not confirmed(rows):
            raise RuntimeError('Unconfirmed attachment creation; checkpoint withheld') from exc
    else:
        rows = odoo.attachments(task_id)
        if not confirmed(rows, attachment_id):
            raise RuntimeError('Attachment checksum or task linkage was not confirmed')
    existing[:] = rows
    return True
