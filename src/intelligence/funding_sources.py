"""Adapters for public funding interfaces. No news classification or writes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime
from functools import partial
import hashlib
import json
import re
import subprocess
import sys
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .classify import fold
from .funding import Opportunity, compare, dated_status
from .network import read_public, validate_url
from .news_html import Element, parse_listing
from .sources import _retry_read
from collections import Counter

TIMEOUT = (5, 8)
MAX_HTML = 4 * 1024 * 1024
MAX_PDF = 5 * 1024 * 1024
FINEP = 'https://www.finep.gov.br'
FINEP_API = FINEP + '/o/c/chamadapublicas'
FAPESP = 'https://fapesp.br/chamadas-html'
PIPE = 'https://fapesp.br/pipe/chamadas'
CNPQ = 'https://www.gov.br/cnpq/pt-br/chamadas'
CNPQ_OPEN = CNPQ + '/Busca_abertas'
CNPQ_DETAIL = '/cnpq/pt-br/chamadas/todas-as-chamadas/'
GARAGEM = 'https://garagem.bndes.gov.br/inscreva-se/'
EMBRAPII = 'https://embrapii.org.br/perguntas-frequentes/'
EMBRAPII_SEBRAE = 'https://embrapii.org.br/sebrae/'
CONFAP = 'https://confap.org.br/pt/editais/'
INOVA = 'https://inova.fiocruz.br/'
TERMINAL = {'closed', 'cancelled', 'suspended'}
DATE_PATTERN = re.compile(r'(?<!\d)(\d{2})/(\d{2})/(\d{4})(?!\d)')
HUMAN_DATE = ('data-limite', 'data limite', 'prazo', 'inscricoes', 'submissao')
PRELIMINARY = ('pre-proposta', 'proposta completa', 'propostas finais', 'propostas enquadradas')


def _safe(base: str, href: str, host: str, path: str) -> str | None:
    try:
        u = urlsplit(urljoin(base, href.strip()))
        if not re.fullmatch(path, u.path) or '%' in u.path and re.search(r'%2f|%5c|%2e', u.path, re.I):
            return None
        target = urlunsplit((u.scheme, u.netloc, quote(u.path, safe="/%:+@-._~$"), u.query, ''))
        validate_url(target, {host})
        return target
    except ValueError:
        return None


def _get(url: str, *, maximum: int = MAX_HTML) -> bytes:
    data, _ = _retry_read(partial(read_public, url, allowed_urls={url}, max_bytes=maximum, timeout=TIMEOUT),
                          Counter())
    return data


def _html(url: str) -> Element:
    return parse_listing(_get(url))


def _short(value: object, length: int = 500) -> str:
    return ' '.join(value.split())[:length] if isinstance(value, str) else ''


def _name(value: object) -> str:
    return _short(value.get('name')) if isinstance(value, dict) else ''


def _iso(value: object) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T[\d:.]+Z)?', value):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _dates(text: str) -> list[date]:
    dates = []
    for m in DATE_PATTERN.finditer(text):
        try:
            dates.append(date(int(m[3]), int(m[2]), int(m[1])))
        except ValueError:
            continue
    return dates


def _lines(node: Element) -> Iterator[str]:
    block = {'h1', 'h2', 'h3', 'h4', 'p', 'li', 'tr', 'br', 'div', 'hr'}
    pending: list[Element | str | None] = list(reversed(node.children))
    words = []
    while pending:
        part = pending.pop()
        if part is None:
            if words:
                yield _short(' '.join(words), 2000)
                words.clear()
        elif isinstance(part, str):
            words.append(part)
        elif part.tag not in ('s', 'strike', 'del'):
            if part.tag == 'tr':
                pending.extend((None, part.text(), None))
                continue
            if part.tag in block:
                pending.append(None)
            pending.extend(reversed(part.children))
            if part.tag in block:
                pending.append(None)
    if words:
        yield _short(' '.join(words), 2000)


def _modality(text: str) -> str:
    normalized = fold(text)
    for code, phrases in (
        ('public_procurement', ('pregao', 'licitacao')),
        ('acceleration', ('aceleracao',)), ('subsidy', ('subvencao economica',)),
        ('investment', ('capital semente', 'fip ', 'investimento em startups')),
        ('credit', ('financiamento reembolsavel', 'credito')),
        ('research_grant', ('auxilio a pesquisa', 'pesquisa inovativa em pequenas empresas', 'pipe')),
        ('scholarship', ('bolsas', 'bolsa ', 'rhae')), ('cooperation', ('cooperacao', 'chamada conjunta')),
        ('technical_support', ('apoio tecnico',)), ('non_reimbursable', ('nao reembolsavel',)),
    ):
        if any(p in normalized for p in phrases):
            return code
    return 'unverified'


def _extract(item: Opportunity, lines: list[str], url: str, section: str, *, page: int | None = None) -> None:
    context = section
    for line in lines:
        normalized = fold(line)
        if re.match(r'^\d+(?:\.\d+)*\)?\s+(?:valor oferecido|cronograma|requisitos)', normalized):
            context = line
        for key, names in (
            ('areas', ('area:', 'areas:', 'areas tematicas:')),
            ('program', ('programa:',)),
            ('official_modality', ('modalidade:', 'modalidades:')),
            ('objective', ('objeto:', 'objetivo:')),
            ('target', ('publico-alvo:', 'publico alvo:', 'elegibilidade:')),
            ('geography', ('abrangencia geografica:', 'regiao:')),
            ('company_size', ('porte:', 'porte da empresa:')),
            ('legal_requirements', ('requisitos juridicos:',)),
            ('trl', ('trl:', 'nivel de maturidade tecnologica:')),
            ('partner_requirement', ('exigencia de ict:', 'parceria obrigatoria:')),
            ('counterpart', ('contrapartida:', 'contrapartida financeira:')),
            ('eligible_expenses', ('despesas financiaveis:', 'itens financiaveis:')),
        ):
            if normalized.startswith(names) and key not in item.facts:
                item.record(key, line.split(':', 1)[1], url, section, page)
        if 'R$' in line or '€' in line:
            amounts = re.findall(r'(?:R\$|€)\s*\d(?:[\d.,]*\d)?(?:\s+(?:milh(?:ão|ões)|mil))?', line, re.I)
            if len(amounts) == 1:
                for key, phrases in (
                    ('global_amount', ('valor global', 'montante global', 'valor total da chamada', 'orcamento da chamada')),
                    ('max_per_proposal', ('valor maximo por proposta', 'valor maximo por projeto')),
                    ('min_per_proposal', ('valor minimo por proposta', 'valor minimo por projeto')),
                ):
                    if (any(phrase in normalized for phrase in phrases)
                            or key == 'global_amount' and 'valor oferecido nesta chamada' in fold(context)
                            or key == 'max_per_proposal' and 'valor maximo de financiamento previsto por projeto' in normalized
                    ) and key not in item.facts:
                        item.record(key, amounts[0], url, context, page)
                        item.record('currency', 'BRL' if 'R$' in amounts[0] else 'EUR', url, context, page)
        dates = _dates(line)
        if normalized.startswith(HUMAN_DATE):
            if item.facts.get('schedule') is None:
                item.record('schedule', line, url, section, page)
            if re.search(r'(?::|em)\s*fluxo continuo\s*[.!]?$', normalized):
                item.record('continuous', 'yes', url, section, page)
            if len(dates) == 2 and normalized.startswith(('inscricoes:', 'submissao de propostas',
                                                           'submissao das propostas', 'periodo de inscricoes:')):
                item.record('applications_start', dates[0].isoformat(), url, section, page)
                item.record('deadline', dates[1].isoformat(), url, section, page)
            elif len(dates) == 1 and normalized.startswith(('prazo', 'data-limite', 'data limite')):
                if 'recurs' not in normalized and not any(word in normalized for word in PRELIMINARY):
                    if 'deadline' not in item.facts:
                        item.record('deadline', dates[0].isoformat(), url, section, page)
                elif any(word in normalized for word in PRELIMINARY) and 'stage_deadline' not in item.facts:
                    item.record('stage_deadline', dates[0].isoformat(), url, section, page)
                if 'pre-proposta' in normalized:
                    item.record('pre_proposal_deadline', dates[0].isoformat(), url, section, page)
                elif any(word in normalized for word in ('proposta completa', 'propostas finais', 'propostas enquadradas')):
                    item.record('full_proposal_deadline', dates[0].isoformat(), url, section, page)
        if re.fullmatch(r'situacao(?: oficial)?: (encerrada|suspensa|cancelada)[.]?', normalized):
            item.record('official_status', line.split(':', 1)[1].strip(' .'), url, section, page)
        if len(dates) == 1 and 'publication_date' not in item.facts and 'publicada em' in normalized:
            item.record('publication_date', dates[0].isoformat(), url, section, page)
        if 'ter sede ou unidade de p&d no estado de sao paulo' in normalized:
            item.record('geography', line, url, context, page)
        if re.fullmatch(r'[a-z]\) ter ate \d+ empregados;', normalized):
            item.record('company_size', line, url, context, page)


def _status(item: Opportunity, today: date) -> None:
    state = item.facts.get('official_status')
    official = fold(state.value) if state else ''
    if official in {'encerrada', 'suspensa', 'cancelada'}:
        item.status = {'encerrada': 'closed', 'suspensa': 'suspended', 'cancelada': 'cancelled'}[official]
    else:
        start, end = (item.facts.get(k) for k in ('applications_start', 'deadline'))
        item.status = dated_status(today, _iso(start.value) if start else None, _iso(end.value) if end else None)
        if item.status != 'deadline_elapsed' and 'continuous' in item.facts:
            item.status = 'continuous'
        elif item.status == 'unverified' and official in ('aberta', 'aberto', 'aberta para submissao'):
            item.status = 'officially_listed_open'
        if item.status == 'deadline_elapsed' and official in ('aberta', 'aberto', 'aberta para submissao'):
            if 'official_open_status_with_elapsed_deadline' not in item.issues:
                item.issues.append('official_open_status_with_elapsed_deadline')
    if item.modality == 'unverified':
        mode = item.facts.get('official_modality')
        item.modality = _modality(mode.value if mode else item.title)


def _pdf(item: Opportunity, url: str) -> None:
    try:
        body = _get(url, maximum=MAX_PDF)
        if not body.startswith(b'%PDF-'):
            raise ValueError('Invalid funding PDF')
        item.documents[url] = hashlib.sha256(body).hexdigest()
        result = subprocess.run(  # noqa: S603 - fixed interpreter/module, PDF bytes through stdin
            [sys.executable, '-m', 'intelligence.funding_pdf'], input=body, capture_output=True,
            timeout=8, check=True,
        )
        pages = json.loads(result.stdout)
        if not any(text.strip() for _, text in pages):
            item.issues.append('pdf_without_extractable_text')
            return
        for page, text in pages:
            _extract(item, text.splitlines(), url, 'PDF', page=page)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
        item.issues.append('pdf_unavailable_or_unreadable')


def _documents(item: Opportunity, content: Element, *, inspect_pdf: bool = False) -> None:
    host = urlsplit(item.url).netloc
    parent = urlsplit(item.url).path.rpartition('/')[0]
    paths = {
        'www.finep.gov.br': r'/documents/[^?#]+',
        'fapesp.br': r'/files/upload/\d+/[^/?#]+|/(?:[^?#]+/)?[^/?#]+\.pdf',
        'www.gov.br': re.escape(parent) + r'/[^/?#]+',
        'garagem.bndes.gov.br': r'/wp-content/uploads/[^?#]+',
        'inova.fiocruz.br': r'/wp-content/uploads/[^?#]+',
        'confap.org.br': r'/(?:news|uploads|storage|arquivos)/[^?#]+',
    }
    if host not in paths:
        return
    targets = []
    for a in content.select('a'):
        label = fold(a.text())
        if not any(word in label for word in ('edital', 'regulamento', 'retifica', 'anexo', 'prorroga')):
            continue
        link = _safe(item.url, a.attrs.get('href', ''), host, paths[host])
        if link and not urlsplit(link).query:
            doc_path = urlsplit(link).path
            if re.search(r'\.(pdf|docx?|xlsx?|odt|zip)(?:/|$)', doc_path, re.I):
                item.documents.setdefault(link, '')
            if 'anexo' not in label and re.search(r'\.pdf(?:/|$)', doc_path, re.I):
                targets.append(link)
        if len(item.documents) >= 24:
            item.issues.append('document_link_limit')
            break
    if inspect_pdf:
        for url in tuple(dict.fromkeys(targets))[:2]:
            _pdf(item, url)


def _details(item: Opportunity, selector: str, *, pdf: bool = False) -> None:
    try:
        root = _html(item.url)
        redirect = next((m.attrs.get('content', '') for m in root.select('meta')
                         if m.attrs.get('http-equiv', '').lower() == 'refresh'), '')
        match = re.fullmatch(r"0;\s*URL=['\"]?([^'\"]+)['\"]?", redirect, re.I)
        if match and urlsplit(item.url).hostname == 'fapesp.br':
            path = re.escape(urlsplit(item.url).path.rstrip('/')) + r'/[^/]+'
            canonical = _safe(item.url, match[1], 'fapesp.br', path)
            if canonical:
                item.url = canonical
                root = _html(canonical)
        content = root.first(selector)
        if content is None:
            raise ValueError('Funding detail layout not recognized')
        _extract(item, list(_lines(content)), item.url, selector)
        if urlsplit(item.url).netloc == 'www.finep.gov.br':
            deadline = content.first('.prazo')
            value = deadline.text() if deadline else ''
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?', value) and _iso(value[:10]):
                item.record('deadline', value[:10], item.url, 'Prazo para envio de propostas até')
                item.record('deadline_as_published', value, item.url, 'Prazo para envio de propostas até')
            if 'recursos de subvencao economica' in fold(content.text()):
                item.modality = 'subsidy'
        _documents(item, content, inspect_pdf=pdf)
    except (ValueError, RuntimeError, OSError):
        item.issues.append('official_detail_unavailable')


def _finep(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    items = {}
    total = None
    seen = set()
    page = 0
    for page in range(1, 11):
        url = f'{FINEP_API}?pageSize=100&page={page}'
        data = json.loads(_get(url))
        rows = data.get('items')
        last = data.get('lastPage')
        count = data.get('totalCount')
        if not isinstance(rows, list) or not isinstance(last, int) or data.get('page') != page or not isinstance(count, int):
            raise ValueError('Finep pagination metadata not recognized')
        if total is not None and count != total:
            raise ValueError('Finep catalogue changed during pagination')
        total = count
        for r in rows:
            raw_id = r.get('id')
            if type(raw_id) not in (str, int) or not re.fullmatch(r'\d{1,15}', str(raw_id)) or str(raw_id) in seen:
                raise ValueError('Invalid or repeated Finep catalogue identity')
            seen.add(str(raw_id))
            identity = f'finep:call:{raw_id}'
            declared = _name(r.get('situacao'))
            end, start = _iso(r.get('vigenciaFim')), _iso(r.get('vigenciaInicio'))
            if identity not in tracked and (fold(declared) in ('encerrada', 'cancelada', 'suspensa')
                                            or (not declared and not end and not start)):
                continue
            title = _short(r.get('titulo'))
            if not title or fold(title).startswith('resultado'):
                continue
            item = Opportunity(identity, 'Finep', title, _modality(_name(r.get('tipoDeOportunidade')) or title),
                               f'{FINEP}/l/{raw_id}')
            for key, v, section in (
                ('official_identifier', str(raw_id), 'id'),
                ('official_status', declared, 'situacao'),
                ('objective', _short(r.get('descricaoRawText'), 1400), 'descricaoRawText'),
                ('geography', _name(r.get('regiao')), 'regiao'),
                ('areas', _name(r.get('temaPrincipal')) or _short(r.get('tema')), 'temaPrincipal / tema'),
                ('target', ', '.join(filter(None, map(_name, r.get('publicoAlvo') or []))), 'publicoAlvo'),
                ('counterpart', _name(r.get('contrapartida')), 'contrapartida'),
                ('official_modality', _name(r.get('tipoDeOportunidade')), 'tipoDeOportunidade'),
            ):
                item.record(key, v, url, section)
            for key, d, section in (('publication_date', _iso(r.get('dataDePublicacao')), 'dataDePublicacao'),
                                    ('valid_from', start, 'vigenciaInicio'), ('valid_through', end, 'vigenciaFim')):
                item.record(key, d.isoformat() if d else None, url, section)
            if fold(title).startswith('finep mais inovacao brasil'):
                item.record('program', 'Mais Inovação Brasil', url, 'titulo')
            _status(item, today)
            if end and end < today and item.status == 'officially_listed_open':
                item.issues.append('official_open_status_with_elapsed_validity')
            items[identity] = item
        if page >= last:
            break
    if len(seen) != total:
        raise ValueError('Incomplete Finep catalogue')
    preferred = sorted(items.values(), key=lambda item: ('saúde / empresas' not in item.title.lower(),
                                                        'coopera' not in item.title.lower(), item.identity))
    def detail(entry: tuple[int, Opportunity]) -> None:
        index, item = entry
        _details(item, '#main-content', pdf=index < 4)
        if not item.documents:
            item.issues.append('document_listing_unverified')
        _status(item, today)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(detail, enumerate(preferred[:40])))
    return list(items.values()), {'pages': page, 'catalogue_rows': len(seen), 'detail_budget': 40}


def _fapesp_rows(document: Element) -> Iterator[tuple[Element, list[str]]]:
    for accordion in document.select('.accordion__item'):
        header = accordion.first('.accordion-header')
        if header is None or 'abertas (todas)' not in fold(header.text()):
            continue
        body = accordion.first('.accordion-body__contents')
        if body is None:
            break
        row: Element | None = None
        content = []
        pending = list(reversed(body.children))
        while pending:
            part = pending.pop()
            if isinstance(part, str):
                if row:
                    content.append(part.strip())
            elif part.tag in ('h3', 'hr'):
                if row:
                    yield row, [line for line in content if line]
                row = part.first('a') if part.tag == 'h3' else None
                content = []
            else:
                pending.extend(reversed(part.children))
        if row:
            yield row, [line for line in content if line]
        return
    raise ValueError('FAPESP open calls section not recognized')


def _fapesp(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    items = {}
    for row, lines in _fapesp_rows(_html(FAPESP)):
        raw = row.attrs.get('href', '').strip()
        if raw.startswith(('http://www.fapesp.br/', 'http://fapesp.br/')):
            raw = 'https://fapesp.br/' + raw.split('/', 3)[-1]
        url = _safe(FAPESP, raw, 'fapesp.br', r'/(?:\d+|acordos-dfg)')
        if not url:
            continue
        ref = urlsplit(url).path.strip('/')
        identity = f'fapesp:call:{ref}'
        title = _short(row.text())
        if not title:
            continue
        item = Opportunity(identity, 'FAPESP', title, _modality(title), url)
        item.record('official_identifier', ref, FAPESP, 'Open calls link')
        item.record('official_status', 'Aberta para submissão', FAPESP, 'Chamadas Abertas')
        if 'pipe' in fold(title):
            item.record('program', 'PIPE', FAPESP, 'Chamadas Abertas')
        _extract(item, lines, FAPESP, 'Chamadas Abertas')
        _status(item, today)
        items[identity] = item
    if not items:
        raise ValueError('FAPESP open calls listing returned no recognized entries')
    preferred = sorted(items.values(), key=lambda i: (
        'pipe' not in fold(i.title) and 'horizon' not in fold(i.title), i.identity,
    ))
    for item in preferred[:5]:
        _details(item, '.page-body', pdf=True)
        _status(item, today)
    return list(items.values()), {'pages': 1, 'detail_budget': 5, 'tracked_only_when_listed': bool(tracked)}


def _cnpq(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    items = {}
    url = CNPQ_OPEN
    for page in range(1, 13):
        root = _html(url)
        anchors = list(root.select('.tileHeadline a'))
        if not anchors:
            raise ValueError('CNPq open calls layout not recognized')
        for anchor in anchors:
            address = _safe(url, anchor.attrs.get('href', ''), 'www.gov.br', CNPQ_DETAIL + r'[\w/-]+')
            if not address or urlsplit(address).query:
                continue
            call = re.search(r'/chamada-no-(\d+)-(\d{4})/', address)
            title = _short(anchor.text())
            if not call or not title:
                continue
            code = f'{call[1]}/{call[2]}'
            item = Opportunity(f'cnpq:call:{code}', 'CNPq', title, _modality(title), address)
            item.record('official_identifier', code, url, 'Open calls list')
            item.record('official_status', 'Aberta para submissão', url, 'Open calls list')
            if 'rhae' in fold(title):
                item.record('program', 'RHAE', url, 'Call title')
            items[item.identity] = item
        expected = f'{CNPQ_OPEN}?b_start:int={page * 5}'
        if not any(a.attrs.get('href') == expected for a in root.select('a.proximo')):
            break
        url = expected
    else:
        raise ValueError('CNPq open calls pagination limit reached')
    if not items:
        raise ValueError('CNPq open calls list has no recognized call identities')
    for index, item in enumerate(items.values()):
        _details(item, '#content-core', pdf=index < 3)
        _status(item, today)
    return list(items.values()), {'pages': page, 'tracked_only_when_listed': bool(tracked)}


def _pipe(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    root = _html(PIPE)
    evidence = next((a.text() for a in root.select('a.link_list')
                     if 'a qualquer momento do ano' in fold(a.text()) and 'pipe fase 1' in fold(a.text())), '')
    if not evidence:
        raise ValueError('PIPE continuous submission terms not recognized')
    items = []
    for label, code in (('Fase 1', 'fase-1'), ('Fase 2 Indireta', 'fase-2-indireta'), ('PIPE Invest', 'invest')):
        if fold(label) not in fold(evidence):
            continue
        item = Opportunity(f'fapesp:pipe:{code}', 'FAPESP', f'PIPE — {label}', 'research_grant', PIPE)
        item.record('program', 'PIPE', PIPE, 'Fluxo Contínuo')
        item.record('official_identifier', label, PIPE, 'Fluxo Contínuo')
        item.record('continuous', 'yes', PIPE, 'Fluxo Contínuo')
        item.record('schedule', evidence, PIPE, 'Fluxo Contínuo')
        _status(item, today)
        items.append(item)
    return items, {'pages': 1}


def _garagem(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    block = _html(GARAGEM).first('.entry-content')
    if not block:
        raise ValueError('BNDES Garagem eligibility and schedule not found')
    lines = list(_lines(block))
    timeframe = next((line for line in lines if fold(line).startswith('inscricoes:') and len(_dates(line)) == 2), '')
    found = re.search(r'Edital[^\n]{0,60}?\b(20\d{2})\b', ' '.join(lines), flags=re.I)
    if not timeframe or not found:
        raise ValueError('BNDES Garagem edition or application dates not found')
    edition = found[1]
    item = Opportunity(f'bndes:garagem:{edition}', 'BNDES', f'BNDES Garagem — Negócios de Impacto {edition}',
                       'acceleration', GARAGEM)
    item.record('program', 'BNDES Garagem', GARAGEM, 'Edital de Negócios de Impacto')
    item.record('official_identifier', edition, GARAGEM, 'Edital de Negócios de Impacto')
    _extract(item, [timeframe], GARAGEM, 'Cronograma')
    for key, marker in (('interviews', 'entrevistas:'), ('acceptance', 'assinatura dos termos de aceite:'),
                        ('programme_start', 'abertura do programa:')):
        evidence = next((line for line in lines if fold(line).startswith(marker)), None)
        item.record(key, evidence, GARAGEM, 'Cronograma')
    for key, marker in (
        ('legal_requirements', 'o edital pede um cnpj ativo'),
        ('target', 'o edital e voltado a organizacoes empresariais'),
    ):
        item.record(key, next((line for line in lines if marker in fold(line)), None), GARAGEM, 'FAQ')
    _documents(item, block, inspect_pdf=True)
    _status(item, today)
    return [item], {'pages': 1, 'tracked_only_when_listed': bool(tracked)}


def _confap(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    items = {}
    url = CONFAP
    for page in range(1, 16):
        root = _html(url)
        cards = list(root.select('div.box'))
        if not cards:
            raise ValueError('CONFAP calls layout not recognized')
        for card in cards:
            title = card.first('h2')
            link = card.first('a.btn')
            state = card.first('p.font-xs')
            if not title or not link or not state:
                continue
            address = _safe(url, link.attrs.get('href', ''), 'confap.org.br', r'/pt/editais/\d+/[a-z0-9-]+')
            if not address:
                continue
            code = urlsplit(address).path.split('/')[3]
            identity = f'confap:call:{code}'
            declared = state.text()
            terminal = {'finalizado': 'closed', 'cancelado': 'cancelled', 'suspenso': 'suspended'}.get(fold(declared))
            if terminal and identity not in tracked:
                continue
            item = Opportunity(identity, 'CONFAP', _short(title.text()), _modality(title.text()), address)
            item.record('official_identifier', code, url, 'Editais')
            item.record('official_status', declared, url, 'Editais')
            if terminal:
                item.status = terminal
            items[identity] = item
        expected = CONFAP + f'pagina={page * 10}'
        if not any(a.attrs.get('href') == expected for a in root.select('.pagination a.page-link')):
            break
        url = expected
    else:
        raise ValueError('CONFAP calls pagination limit reached')
    for item in list(items.values())[:8]:
        if item.status not in TERMINAL:
            _details(item, 'body')
            _status(item, today)
    return list(items.values()), {'pages': page, 'detail_budget': 8}


def _embrapii(today: date, tracked: set[str], *, sebrae: bool = False) -> tuple[list[Opportunity], dict[str, Any]]:
    url = EMBRAPII_SEBRAE if sebrae else EMBRAPII
    root = _html(url)
    lines = list(_lines(root))
    if sebrae:
        scope = root.first('.como-funciona')
        terms = scope.first('.description-section') if scope is not None else None
        target = root.first('.quem-pode-ter-apoio')
        if not terms or not target or 'unidade embrapii' not in fold(terms.text()):
            raise ValueError('EMBRAPII/Sebrae participation rules not recognized')
        item = Opportunity('embrapii:program:sebrae', 'EMBRAPII / Sebrae', 'EMBRAPII e Sebrae — apoio a MPEs',
                           'non_reimbursable', url, status='application_route_verified')
        item.record('counterpart', terms.text(), url, 'Como funciona')
        requirement = target.first('.description-wrapper-with-bg')
        if requirement is not None:
            item.record('company_size', requirement.text(), url, 'Quem pode ter o apoio do Sebrae?')
        evidence = terms.text()
    else:
        evidence = next((line for line in lines if 'empresa contratante do projeto' in fold(line)
                         and 'unidade embrapii' in fold(line)), '')
        if not evidence:
            raise ValueError('EMBRAPII contractual arrangement not recognized')
        item = Opportunity('embrapii:program:business-rdi', 'EMBRAPII', 'Projetos de PD&I com unidade EMBRAPII',
                           'non_reimbursable', url, status='application_route_verified')
    item.record('program', 'EMBRAPII/Sebrae' if sebrae else 'EMBRAPII', url, 'Official programme')
    item.record('partner_requirement', evidence, url, 'Como funciona' if sebrae else 'Perguntas Frequentes')
    item.compatibility = 'conditional_partnership'
    return [item], {'pages': 1, 'scope': 'programme_rules'}


def _fiocruz(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    root = _html(INOVA)
    block = root.first('.elementor-element-2ae7add')
    if not block:
        raise ValueError('Programa Inova open submission section not recognized')
    items = []
    for a in block.select('a'):
        url = _safe(INOVA, a.attrs.get('href', ''), 'inova.fiocruz.br', r'/edital/[a-z0-9-]+/')
        if not url:
            continue
        code = urlsplit(url).path.split('/')[2]
        item = Opportunity(f'fiocruz:inova:{code}', 'Fiocruz', _short(a.text()), 'unverified', url)
        if not item.title or any(previous.identity == item.identity for previous in items):
            continue
        item.record('official_identifier', code, INOVA, 'Chamadas abertas para submissão')
        item.record('program', 'Programa Inova', INOVA, 'Chamadas abertas para submissão')
        item.record('official_status', 'Aberta para submissão', INOVA, 'Chamadas abertas para submissão')
        _details(item, 'main', pdf=len(items) < 2)
        _status(item, today)
        items.append(item)
    if not items and 'nenhum edital aberto' not in fold(block.text()):
        raise ValueError('Programa Inova submission section has no recognized status')
    if 'fiocruz:inova:edital-saude-digital' in tracked:
        url = INOVA + 'edital/edital-saude-digital/'
        body = _html(url)
        if 'submissao encerrada' in fold(body.text()):
            item = Opportunity('fiocruz:inova:edital-saude-digital', 'Fiocruz', 'Edital Saúde Digital',
                               'unverified', url, status='closed')
            item.record('official_status', 'Submissão encerrada', url, 'Submissão')
            items.append(item)
    return items, {'pages': 1, 'confirmed_empty': not items, 'scope': 'Inova open submission section'}


INOVACRED = FINEP + '/credito-operado-por-agentes'
BNDES_CREDIT = ('https://www.bndes.gov.br/wps/portal/site/home/financiamento/produto/'
                'programa-bndes-mais-inovacao-investimento')
FAPERJ = 'https://www.faperj.br/?id=28.5.7'
TECNOVA_PE = 'https://tecnova.pe.gov.br/'
SENAI = ('https://www.portaldaindustria.com.br/canais/plataforma-inovacao-para-industria/'
         'categoria/chamada-regional-senai/')
SENAI_REGULATION = ('https://static.portaldaindustria.com.br/media/filer_public/cb/b8/'
                    'cbb8dabd-b7cd-4bab-868e-406ebbc50d70/'
                    'plataforma_inovacao_2026_30032026_-_atualizacao_03112026.pdf')
CENTELHA_SP_RULES = ('https://fapesp.br/18109/'
                     'programa-nacional-de-apoio-a-geracao-de-empreendimentos-inovadores-programa-centelha-3-sao-paulo')
PPSUS = 'https://www.gov.br/saude/pt-br/composicao/sectics/ppsus'
RHAE = 'https://www.gov.br/cnpq/pt-br/acesso-a-informacao/acoes-e-programas/programas/rhae'


def _credit(today: date, tracked: set[str], *, bndes: bool = False) -> tuple[list[Opportunity], dict[str, Any]]:
    url = BNDES_CREDIT if bndes else INOVACRED
    root = _html(url)
    if bndes:
        text = next((p.text() for p in root.select('p') if 'financiamento direto com o bndes, a partir de' in
                     fold(p.text()) and 'demais regioes' in fold(p.text())), '')
        if not text:
            raise ValueError('BNDES regional credit thresholds not recognized')
        item = Opportunity('bndes:program:mais-inovacao-investimento', 'BNDES',
                           'BNDES Mais Inovação — Investimento em inovação', 'credit', url,
                           status='application_route_verified')
        item.record('min_credit_by_region', text, url, 'Valor mínimo de financiamento')
        item.record('currency', 'BRL', url, 'Valor mínimo de financiamento')
    else:
        part = root.first('#acc-0-credito-operado-por-parceiros/finep-inovacred')
        if part is None:
            raise ValueError('Inovacred credit terms table not recognized')
        item = Opportunity('finep:program:inovacred', 'Finep', 'Finep Inovacred', 'credit', url,
                           status='application_route_verified')
        rows = [' | '.join(cell.text() for cell in row.select('td')) for row in part.select('tr')]
        terms = ' / '.join(row for row in rows if row)
        if 'R$' not in terms:
            raise ValueError('Inovacred table did not provide finance terms')
        item.record('credit_terms_by_size', terms, url, 'Finep Inovacred / Porte e Valor')
        item.record('currency', 'BRL', url, 'Finep Inovacred / Valor')
    return [item], {'pages': 1, 'scope': 'product terms'}


def _faperj(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    root = _html(FAPERJ)
    region = root.first('section.corpo-interna')
    heading = region.first('h1') if region else None
    if not region or not heading or 'lista de editais' not in fold(heading.text()):
        raise ValueError('FAPERJ official calls calendar not recognized')
    items = {}
    for paragraph in region.select('p'):
        for anchor in paragraph.select('a'):
            title = _short(anchor.text())
            code = re.match(r'Edital FAPERJ N[º°]\s*(\d+)/(\d{4})', title, re.I)
            href = _safe(FAPERJ, anchor.attrs.get('href', ''), 'www.faperj.br', r'/rp/downloads/[^/]+\.pdf')
            if not code or not href or urlsplit(href).query:
                continue
            identity = f'faperj:call:{int(code[1])}/{code[2]}'
            item = Opportunity(identity, 'FAPERJ', title, _modality(title), FAPERJ)
            item.record('official_identifier', identity.rsplit(':', 1)[1], FAPERJ, 'Lista de editais')
            item.documents[href] = ''
            _extract(item, list(_lines(paragraph)), FAPERJ, 'Cronograma da chamada')
            _status(item, today)
            if item.status != 'deadline_elapsed' or identity in tracked:
                items.setdefault(identity, item)
    if not items:
        raise ValueError('No FAPERJ call observations recognized')
    for item in [item for item in items.values() if item.status == 'within_submission_window'][:3]:
        _pdf(item, next(iter(item.documents)))
        _status(item, today)
    return list(items.values()), {'pages': 1, 'scope': 'official calendar', 'detail_budget': 3}


def _tecnova(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    root = _html(TECNOVA_PE)
    lines = [row.text() for row in root.select('.ue_timeline_item')]
    evidence = next((line for line in lines if fold(line).startswith('termino do prazo para envio eletronico')
                     and len(_dates(line)) == 1), '')
    if not evidence:
        raise ValueError('Tecnova PE submission deadline not recognized')
    year = _dates(evidence)[0].year
    item = Opportunity(f'tecnova:pe:{year}', 'FACEPE / SECTI PE / Finep', f'Tecnova PE — {year}',
                       'subsidy', TECNOVA_PE)
    item.record('program', 'Tecnova', TECNOVA_PE, 'Official programme')
    item.record('deadline', _dates(evidence)[0].isoformat(), TECNOVA_PE, 'Cronograma')
    item.record('schedule', evidence, TECNOVA_PE, 'Cronograma')
    _status(item, today)
    return [item], {'pages': 1, 'scope': 'Pernambuco edition'}


def _centelha(today: date, tracked: set[str], *, state: str) -> tuple[list[Opportunity], dict[str, Any]]:
    if state == 'sp':
        content = _html(CENTELHA_SP_RULES).first('.page-body')
        if content is None:
            raise ValueError('Centelha SP regulation not recognized')
        lines = list(_lines(content))
        phase = next((line for line in lines if fold(line).startswith('fase 1. submissao das ideias')
                      and len(_dates(line)) == 2), '')
        if not phase:
            raise ValueError('Centelha SP first-stage schedule not recognized')
        item = Opportunity('centelha:sp:2026', 'FAPESP', 'Programa Centelha 3 — São Paulo', 'subsidy',
                           CENTELHA_SP_RULES)
        start, deadline = _dates(phase)
        item.record('official_identifier', '18109', CENTELHA_SP_RULES, 'Regulation')
        item.record('program', 'Centelha', CENTELHA_SP_RULES, 'Regulation')
        item.record('applications_start', start.isoformat(), CENTELHA_SP_RULES, 'Cronograma / Fase 1')
        item.record('deadline', deadline.isoformat(), CENTELHA_SP_RULES, 'Cronograma / Fase 1')
        item.record('schedule', phase, CENTELHA_SP_RULES, 'Cronograma / Fase 1')
        for key, marker in (('global_amount', '2.1.'), ('max_per_proposal', '2.2.')):
            evidence = next((line for line in lines if line.startswith(marker) and 'R$' in line), '')
            amount = re.search(r'R\$\s*[\d.]+,\d{2}', evidence)
            if amount:
                item.record(key, amount[0], CENTELHA_SP_RULES, f'Regulation / {marker}')
                item.record('currency', 'BRL', CENTELHA_SP_RULES, f'Regulation / {marker}')
        _documents(item, content)
        _status(item, today)
        return [item], {'pages': 1, 'scope': 'state regulation and schedule'}
    url = f'https://programacentelha.com.br/{state}/'
    document = _html(url)
    launch = [(link, a.text()) for a in document.select('a')
              if 'edital de lancamento' in fold(a.text()) and (
                  link := _safe(url, a.attrs.get('href', ''), 'drive.google.com', r'/file/d/[\w-]+/view')
              )]
    if len(launch) != 1:
        raise ValueError('Centelha state launch notice not recognized')
    doc, evidence = launch[0]
    year = re.search(r'\b20\d{2}\b', evidence)
    if not year:
        raise ValueError('Centelha edition year not recognized')
    identity = 'faperj:call:12/2026' if state == 'rj' and year[0] == '2026' else f'centelha:{state}:{year[0]}'
    item = Opportunity(identity, 'FAPERJ' if state == 'rj' else 'Programa Centelha',
                       f'Centelha — {state.upper()} {year[0]}', 'subsidy', url)
    item.record('program', 'Centelha', url, 'Edital de lançamento')
    item.record('edition', evidence, url, 'Edital de lançamento')
    item.documents[doc.split('?', 1)[0]] = ''
    item.issues.append('external_document_link_requires_review')
    return [item], {'pages': 1, 'scope': 'state edition and notice links'}


def _programme(today: date, tracked: set[str], *, code: str, title: str, institution: str,
               url: str, marker: str, modality: str) -> tuple[list[Opportunity], dict[str, Any]]:
    root = _html(url)
    evidence = next((p.text() for p in root.select('p') if marker in fold(p.text())), '')
    if not evidence:
        raise ValueError('Official programme evidence was not found')
    item = Opportunity(f'{code}:program', institution, title, modality, url)
    item.record('program', title, url, 'Official programme')
    item.record('programme_rules', evidence, url, 'Official programme')
    return [item], {'pages': 1, 'scope': 'programme rules only'}


def _senai(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    item = Opportunity('senai:platform:2026', 'SENAI', 'Plataforma Inovação para a Indústria — 2026',
                       'unverified', SENAI)
    _pdf(item, SENAI_REGULATION)
    if item.issues or SENAI_REGULATION not in item.documents:
        raise ValueError('SENAI official regulation could not be verified')
    item.record('program', item.title, SENAI_REGULATION, 'Regulamento geral e categorias', 1)
    try:
        rows = list(_lines(_html(SENAI)))
        terms = next((line for line in rows if 'departamento regional e responsavel pela chamada regional' in
                      fold(line)), None)
        item.record('programme_rules', terms, SENAI, 'Chamada Regional SENAI')
    except (ValueError, RuntimeError):
        item.issues.append('regional_listing_unavailable')
    return [item], {'pages': 1, 'scope': '2026 programme regulation only'}


FUNDING_ADAPTERS: dict[str, tuple[str, Callable[..., tuple[list[Opportunity], dict[str, Any]]]]] = {
    'finep_mcti_fndct': (FINEP_API, _finep),
    'finep_inovacred': (INOVACRED, _credit),
    'fapesp_general': (FAPESP, _fapesp),
    'fapesp_pipe_continuous': (PIPE, _pipe),
    'cnpq': (CNPQ_OPEN, _cnpq),
    'embrapii': (EMBRAPII, _embrapii),
    'embrapii_sebrae': (EMBRAPII_SEBRAE, partial(_embrapii, sebrae=True)),
    'bndes_garagem': (GARAGEM, _garagem),
    'bndes_credit': (BNDES_CREDIT, partial(_credit, bndes=True)),
    'confap': (CONFAP, _confap),
    'fiocruz_inova': (INOVA, _fiocruz),
    'faperj': (FAPERJ, _faperj),
    'tecnova_pe': (TECNOVA_PE, _tecnova),
    'centelha_sp': (CENTELHA_SP_RULES, partial(_centelha, state='sp')),
    'centelha_rj': ('https://programacentelha.com.br/rj/', partial(_centelha, state='rj')),
    'cnpq_rhae': (RHAE, partial(_programme, code='cnpq:rhae', title='RHAE', institution='CNPq', url=RHAE,
                                marker='programa de formacao de recursos humanos em areas estrategicas',
                                modality='scholarship')),
    'decit_ppsus': (PPSUS, partial(_programme, code='decit:ppsus', title='Programa Pesquisa para o SUS',
                                 institution='Ministério da Saúde / DECIT', url=PPSUS,
                                 marker='programa pesquisa para o sus', modality='research_grant')),
    'senai_regional': (SENAI, _senai),
}


def collect_opportunities(today: date, tracked: set[str]) -> tuple[list[Opportunity], dict[str, Any]]:
    def collect(entry) -> tuple[str, list[Opportunity], dict[str, Any]]:
        code, (url, adapter) = entry
        try:
            items, metadata = adapter(today, tracked)
            partial_result = any(item.issues for item in items)
            state = 'partial' if partial_result else 'verified' if items else 'verified_empty'
            return code, items, {'endpoint': url, 'state': state, 'count': len(items), **metadata}
        except (RuntimeError, ValueError, OSError, TypeError, AttributeError, KeyError) as exc:
            return code, [], {'endpoint': url, 'state': 'unavailable', 'count': 0,
                              'error': f'{type(exc).__name__}: {exc}'[:240]}

    sources = {}
    items: dict[str, Opportunity] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for code, batch, audit in pool.map(collect, FUNDING_ADAPTERS.items()):
            sources[code] = audit
            for item in batch:
                if item.identity not in items:
                    items[item.identity] = item
                else:
                    audit['duplicates'] = audit.get('duplicates', 0) + 1
                    canonical = items[item.identity]
                    for key, value in item.facts.items():
                        canonical.facts.setdefault(key, value)
                    for url, digest in item.documents.items():
                        if not canonical.documents.get(url):
                            canonical.documents[url] = digest
    return list(items.values()), sources


def preview(previous: list[Opportunity]) -> dict[str, Any]:
    now = datetime.now(ZoneInfo('America/Sao_Paulo'))
    items, sources = collect_opportunities(now.date(), {item.identity for item in previous})
    for item in items:
        item.checked_at = now.isoformat(timespec='seconds')
    return {
        'mode': 'funding-preview', 'checked_at': now.isoformat(timespec='seconds'),
        'status': 'failed' if all(source['state'] == 'unavailable' for source in sources.values())
        else 'partial' if any(source['state'] in ('unavailable', 'partial') for source in sources.values())
        else 'success',
        'sources': sources, 'opportunities': [asdict(item) for item in items],
        'changes': compare(previous, items),
    }
