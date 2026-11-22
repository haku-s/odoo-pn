"""Attribution paired with photographs on the Chamber's official news channels."""

from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from .network import validate_url

HOST = 'www.camara.leg.br'
PUBLISHER = 'Câmara dos Deputados'


def is_article(url: str) -> bool:
    try:
        validate_url(url, {HOST})
    except ValueError:
        return False
    return re.fullmatch(r'/noticias/[0-9]+-[a-z0-9-]+/?', urlsplit(url).path) is not None


def photo_credit(value: str | None) -> str | None:
    if not value:
        return None
    author, _, publisher = ' '.join(value.split()).removeprefix('Foto: ').partition('/')
    author = author.strip()
    # Official reuse policy requires the photographer and the Chamber.
    if publisher.strip() == PUBLISHER and 0 < len(author) <= 100 and author.isprintable():
        return f'{author} / {PUBLISHER}'
    return None


def eligible_photo(article: str, image: str | None, credit: str | None) -> bool:
    if not image or not is_article(article) or not photo_credit(credit):
        return False
    try:
        validate_url(image, {HOST})
    except ValueError:
        return False
    path = urlsplit(image).path
    return (
        path.startswith('/midias/image/')
        and re.fullmatch(r'/midias/image/[a-zA-Z0-9/_-]+\.(?:jpe?g|png|webp)', path, re.IGNORECASE) is not None
    )


class _Media(HTMLParser):
    def __init__(self, article: str) -> None:
        super().__init__()
        self.article = article
        self.photo: tuple[str, str] | None = None
        self.container = ''
        self.depth = 0
        self.credit_tag = ''
        self.credits: list[str] = []
        self.images: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.photo is not None:
            return
        attrs_by_name = dict(attrs)
        classes = set((attrs_by_name.get('class') or '').split())
        if not self.container:
            if (tag == 'div' and 'image-container' in classes) or (
                tag == 'figure' and 'materia__midia' in classes
            ):
                if (attrs_by_name.get('data-midia') or '').isdigit():
                    self.container, self.depth = tag, 1
                    self.credits.clear()
                    self.images.clear()
            return
        if tag == self.container:
            self.depth += 1
        if classes.intersection({'midia-creditos', 'materia__creditos-imagem'}):
            self.credit_tag = tag
        if tag == 'img' and (src := attrs_by_name.get('src')):
            self.images.add(src)

    def handle_data(self, data: str) -> None:
        if self.credit_tag:
            self.credits.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self.credit_tag:
            self.credit_tag = ''
        if tag != self.container:
            return
        self.depth -= 1
        if self.depth:
            return
        self.container = self.credit_tag = ''
        if len(self.images) == 1 and (credit := photo_credit(' '.join(self.credits))):
            image = urljoin(self.article, next(iter(self.images)))
            if eligible_photo(self.article, image, credit):
                self.photo = image, credit


def credited_photo(article: str, markup: str) -> tuple[str, str] | None:
    if not is_article(article):
        return None
    parser = _Media(article)
    parser.feed(markup)
    return parser.photo
