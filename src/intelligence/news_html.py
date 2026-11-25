"""Read publisher-maintained public news listings with explicit routes and selectors."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterator
from urllib.parse import urljoin, urlsplit


@dataclass(frozen=True)
class Listing:
    url: str
    rows: str
    title: str
    date: str
    summary: str
    article_path: str
    next_page: str = ''
    step: int = 1
    first_next: int = 1
    link: str = 'a'


GOV = 'https://www.gov.br'
GOV_DATE = '.documentByLine'
GOV_COLLECTION = '.tileItem', '.tileHeadline', GOV_DATE, '.tileBody'
GOV_FOLDER = '.listagem-noticias-com-foto li', '.titulo', '.data', '.descricao'


def gov_listing(path: str, article_path: str, *, folder: bool = False, step: int = 30) -> Listing:
    url = GOV + path
    return Listing(url, *(GOV_FOLDER if folder else GOV_COLLECTION), article_path,
                   next_page=url + '?b_start:int={}', step=step)


EBC_ARCHIVE = Listing(
    'https://agenciabrasil.ebc.com.br/ultimas', '.ultima-noticia', 'a.titulo-noticia',
    '.data-publicacao', '.resumo-noticia', r'/[^/]+/noticia/\d{4}-\d{2}/[^/]+',
    next_page='https://agenciabrasil.ebc.com.br/ultimas?page={}',
)
FIOCRUZ_ARCHIVE = Listing(
    'https://agencia.fiocruz.br/noticias', '.afn-noticias', 'h3', 'time', '.col-md-9',
    r'/[a-z0-9][a-z0-9-]+', next_page='https://agencia.fiocruz.br/noticias?page={}',
)
HEALTH_LISTING = gov_listing('/saude/pt-br/assuntos/noticias',
                            r'/saude/pt-br/assuntos/noticias/\d{4}/[^/]+/[^/]+', step=15)

HTML_CHANNELS = {
    'ministerio_saude': HEALTH_LISTING,
    'mec': Listing(GOV + '/mec/pt-br/assuntos/noticias', '.cover-collection-tile .collection-item',
                   'h3', 'time', '.description', r'/mec/pt-br/assuntos/noticias/\d{4}/[^/]+/[^/]+'),
    'mte': gov_listing('/trabalho-e-emprego/pt-br/noticias-e-conteudo',
                       r'/trabalho-e-emprego/pt-br/noticias-e-conteudo/\d{4}/[^/]+/[^/]+', folder=True),
    'inep': gov_listing('/inep/pt-br/centrais-de-conteudo/noticias',
                        r'/inep/pt-br/centrais-de-conteudo/noticias/[^/]+/[^/]+', folder=True),
    'mcti': gov_listing('/mcti/pt-br/acompanhe-o-mcti/noticias',
                        r'/mcti/pt-br/acompanhe-o-mcti/noticias/\d{4}/(?:[^/]+/)?[^/]+'),
    'inpi': gov_listing('/inpi/pt-br/central-de-conteudo/noticias/ultimas-noticias',
                        r'/inpi/pt-br/central-de-conteudo/noticias/[^/]+'),
    'cnpq': gov_listing('/cnpq/pt-br/assuntos/noticias',
                        r'/cnpq/pt-br/assuntos/noticias/[^/]+(?:/[^/]+)*', folder=True),
    'opas': Listing('https://www.paho.org/pt/brasil', '#views-bootstrap-vistas-oficina-block-1 .col',
                    '.views-field-title', 'time', '', r'/pt/noticias/\d{2}-\d{2}-\d{4}-[^/]+'),
    'agencia_fapesp': Listing('https://agencia.fapesp.br/noticias', '.newsList li', '.card-title',
                              '.subtitle', '.description', r'/[^/]+/\d+',
                              next_page='https://agencia.fapesp.br/noticias/{}', first_next=2),
    'pesquisa_inovacao_fapesp': Listing('https://pesquisaparainovacao.fapesp.br/noticias/pg/1',
                                       '._news li.item', '.title', '.subtitle', '.txt', r'/[^/]+/\d+',
                                       next_page='https://pesquisaparainovacao.fapesp.br/noticias/pg/{}', first_next=2),
    'fapesp': Listing('https://fapesp.br/index.php/noticias/1', '.newsList li', 'h3', '.datePublic',
                      '.description', r'/index\.php/\d+/[^/]+',
                      next_page='https://fapesp.br/index.php/noticias/{}', first_next=2),
    'finep': Listing('https://www.finep.gov.br/noticias', '.bloco-scroll-home', 'h2',
                     '.hora-publ', '.description', r'/w/[^/]+', link='.link-to-not a'),
    'agencia_bndes': Listing('https://agenciadenoticias.bndes.gov.br/busca', '.cards-list a.card',
                            '.card--title', '.card--date', '.card--content p', r'/noticia/[^/]+',
                            next_page='https://agenciadenoticias.bndes.gov.br/busca?page={}', first_next=2),
}

# These are alternate public interfaces for existing subscriptions.
ARCHIVES = {entry.url: entry for entry in (EBC_ARCHIVE, FIOCRUZ_ARCHIVE, *HTML_CHANNELS.values())}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    children: list[Element | str] = field(default_factory=list)

    def matches(self, selector: str) -> bool:
        tag, _, marker = selector.partition('.')
        if tag.startswith('#'):
            return self.attrs.get('id') == tag[1:]
        return (not tag or self.tag == tag) and (not marker or marker in self.attrs.get('class', '').split())

    def select(self, selector: str) -> Iterator[Element]:
        if not selector:
            return
        match, _, tail = selector.partition(' ')
        pending = list(reversed(self.children))
        while pending:
            child = pending.pop()
            if isinstance(child, str):
                continue
            if child.matches(match):
                if tail:
                    yield from child.select(tail)
                else:
                    yield child
            pending.extend(reversed(child.children))

    def first(self, selector: str) -> Element | None:
        return next(self.select(selector), None)

    def text(self) -> str:
        pending = list(reversed(self.children))
        chunks = []
        while pending:
            child = pending.pop()
            if isinstance(child, str):
                chunks.append(child)
            else:
                pending.extend(reversed(child.children))
        return ' '.join(' '.join(chunks).split())


class ListingParser(HTMLParser):
    VOID = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
                      'meta', 'param', 'source', 'track', 'wbr'))

    def __init__(self):
        super().__init__()
        self.root = Element('', {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Element(tag, {key: value for key, value in attrs if value is not None})
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack[-1].tag not in ('script', 'style') and data.strip():
            self.stack[-1].children.append(data)


def parse_listing(body: bytes) -> Element:
    parser = ListingParser()
    parser.feed(body.decode('utf-8-sig'))
    parser.close()
    return parser.root


def next_listing_page(listing: Listing, document: Element, current: str, page: int) -> str | None:
    if not listing.next_page:
        return None
    expected = listing.next_page.format(page)
    for anchor in document.select('a'):
        try:
            if urljoin(current, anchor.attrs.get('href', '')) == expected:
                return expected
        except ValueError:
            continue
    return None


_MONTHS = tuple('janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro'.split())
_DMY = re.compile(r'(?<!\d)(\d{2}/\d{2}/(?:\d{4}|\d{2}))(?:\s*(?:-|às)?\s*(\d{1,2})[h:](\d{2}))?')
_LOCAL_DATE = re.compile(r'(\d{1,2}) de ([^ ]+) de (\d{4})', re.IGNORECASE)


def listing_date(element: Element | None) -> str | None:
    if element is None:
        return None
    if value := element.attrs.get('datetime'):
        return value
    text = element.text()
    if match := _DMY.search(text):
        day, hour, minute = match.groups()
        if len(day) == 8:
            day = day[:6] + '20' + day[6:]
        return f'{day} {int(hour):02}:{minute}' if hour else day
    if (match := _LOCAL_DATE.search(text)) and match[2].lower() in _MONTHS:
        return f'{int(match[1]):02}/{_MONTHS.index(match[2].lower()) + 1:02}/{match[3]}'
    return None


def in_article_scope(listing: Listing, link: str) -> bool:
    parts = urlsplit(link)
    return not parts.query and re.fullmatch(listing.article_path, parts.path.rstrip('/')) is not None
