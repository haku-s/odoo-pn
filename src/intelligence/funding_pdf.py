"""Extract a bounded PDF text sample in an isolated, short-lived process."""

from io import BytesIO
import json
import resource
import sys

from pypdf import PdfReader

MAX_PDF_BYTES = 5 * 1024 * 1024


def main() -> None:
    memory_limit = 384 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    resource.setrlimit(resource.RLIMIT_CPU, (4, 5))
    body = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
    if len(body) > MAX_PDF_BYTES or not body.startswith(b'%PDF-'):
        raise ValueError('Invalid funding PDF')
    reader = PdfReader(BytesIO(body))
    if reader.is_encrypted:
        raise ValueError('Encrypted funding PDF')
    pages = [(i + 1, (page.extract_text() or '')[:20000]) for i, page in enumerate(reader.pages[:8])]
    print(json.dumps(pages, ensure_ascii=True))


if __name__ == '__main__':
    main()
