"""Collect bounded scientific metadata and reconcile records by official identifiers."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from xml.etree import ElementTree as ET

from .classify import fold
from .network import read_public, validate_url
from .news_html import ListingParser
from .sources import _retry_read

PUBMED = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
CROSSREF = 'https://api.crossref.org/works'
SCIELO = 'https://articlemeta.scielo.org/api/v1/article/'
OPENALEX = 'https://api.openalex.org/works'
RADARS = 'https://inpidata.inpi.gov.br/Radares_govbr.html'
RADAR_ARCHIVE = 'https://www.gov.br/inpi/pt-br/assuntos/informacao/radares-tecnologicos'
OVERLAP = 14
MAX_PERIOD = 31
MAX_BYTES = 4 * 1024 * 1024
MONTHS: tuple[str, ...] = tuple('jan feb mar apr may jun jul aug sep oct nov dec'.split())
PT_MONTHS: tuple[str, ...] = tuple('jan fev mar abr mai jun jul ago set out nov dez'.split())
NLM_DTD = re.compile(
    r'(?m)^<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, [^"<>\[\]]+//EN"\s+'
    r'"https://dtd\.nlm\.nih\.gov/ncbi/pubmed/out/pubmed_[0-9]{6}\.dtd">\s*',
)
PMID = re.compile(r'[1-9][0-9]{0,11}')
DOI = re.compile(r'10\.\d{4,9}/[^\s<>"\x00-\x1f\x7f]+', re.ASCII)
NOTICE_TYPES = {
    'Published Erratum': 'correction', 'Retraction of Publication': 'retraction',
    'Expression of Concern': 'expression_of_concern', 'Corrected and Republished Article': 'republication',
}
KINDS = {
    'Preprint': 'preprint', 'Systematic Review': 'systematic_review', 'Meta-Analysis': 'meta_analysis',
    'Randomized Controlled Trial': 'clinical_trial', 'Clinical Trial': 'clinical_trial',
    'Observational Study': 'observational_study', 'Review': 'review', 'Technical Report': 'technical_report',
    'Journal Article': 'article',
}
DISTINCT_KINDS = {'preprint', 'correction', 'retraction', 'expression_of_concern', 'republication'}
REUSABLE_LICENSE = re.compile(
    r'https?://creativecommons\.org/(?:licenses/by/[1-4]\.0|publicdomain/zero/1\.0)(?:/legalcode)?/?',
)
Row = dict[str, Any]
Observations = list[tuple[str, Row]]


@dataclass(frozen=True)
class Search:
    key: str
    pubmed: str
    english: tuple[str, ...]
    portuguese: tuple[str, ...]


SEARCHES = (
    Search('speech_language_markers',
           '("spontaneous speech"[Title/Abstract] OR "speech graph"[Title/Abstract]'
           ' OR "speech graphs"[Title/Abstract] OR "linguistic markers"[Title/Abstract]'
           ' OR "language sample analysis"[Title/Abstract])'
           ' AND ("Cognition"[MeSH Terms] OR "Mental Health"[MeSH Terms] OR cognit*[Title/Abstract]'
           ' OR literacy[Title/Abstract] OR "mental health"[Title/Abstract])',
           ('spontaneous speech cognitive', 'speech graph mental health'), ('fala espontânea cognição',)),
    Search('neurocognitive_assessment',
           '("Neuropsychological Tests"[MeSH Terms] OR "neuropsychological assessment"[Title/Abstract]'
           ' OR "neurocognitive assessment"[Title/Abstract])',
           ('neuropsychological assessment', 'neurocognitive assessment'),
           ('avaliação neuropsicológica', 'avaliação neurocognitiva')),
    Search('psychometrics',
           '("Psychometrics"[MeSH Terms] OR psychometric*[Title/Abstract])'
           ' AND ("cognitive assessment"[Title/Abstract] OR literacy[Title/Abstract]'
           ' OR "mental health"[Title/Abstract] OR neuropsycholog*[Title/Abstract])',
           ('psychometric cognitive assessment', 'psychometric mental health'), ('validade psicométrica',)),
    Search('learning_assessment',
           '("educational assessment"[Title/Abstract] OR "reading assessment"[Title/Abstract]'
           ' OR "literacy assessment"[Title/Abstract])'
           ' AND ("Schools"[MeSH Terms] OR "Students"[MeSH Terms] OR classroom*[Title/Abstract]'
           ' OR "primary school"[Title/Abstract] OR "elementary school"[Title/Abstract])'
           ' NOT "health literacy"[Title]',
           ('literacy assessment', 'reading assessment'), ('avaliação da aprendizagem',)),
    Search('occupational_mental_health',
           '("Occupational Health"[MeSH Terms] OR workplace[Title/Abstract] OR occupational[Title/Abstract])'
           ' AND ("Mental Health"[MeSH Terms] OR "psychosocial risk"[Title/Abstract]'
           ' OR "psychosocial risks"[Title/Abstract])',
           ('occupational mental health', 'psychosocial risks at work'), ('riscos psicossociais no trabalho',)),
    Search('applied_ai_assistive_technology',
           '("Artificial Intelligence"[MeSH Terms] OR "artificial intelligence"[Title/Abstract]'
           ' OR "Self-Help Devices"[MeSH Terms] OR "assistive technology"[Title/Abstract])'
           ' AND ("cognitive assessment"[Title/Abstract] OR "reading assessment"[Title/Abstract]'
           ' OR "educational assessment"[Title/Abstract] OR "neuropsychological assessment"[Title/Abstract])',
           ('assistive technology cognitive assessment', 'artificial intelligence educational assessment'),
           ('tecnologia assistiva avaliação educacional',)),
)


def read(endpoint: str, params: dict[str, str | int], audit: Counter[str]) -> bytes:
    url = endpoint + ('?' + urlencode(params) if params else '')
    # Keyless calls stay below the NCBI shared three requests per second limit.
    if endpoint.startswith(PUBMED):
        sleep(0.4)
    elif endpoint == CROSSREF:
        sleep(0.25)
    audit['requests'] += 1
    body, _ = _retry_read(lambda: read_public(
        url, allowed_urls={url}, max_bytes=MAX_BYTES, timeout=(4, 8),
    ), audit)
    return body


def doi(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r'^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)', '', value.strip(), flags=re.IGNORECASE)
    normalized = unquote(value).lower()
    return normalized if len(normalized) <= 300 and DOI.fullmatch(normalized) else None


def xml_records(body: bytes) -> list[ET.Element]:
    payload = NLM_DTD.sub('', body.decode('utf-8-sig'), count=1)
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', payload, re.IGNORECASE):
        raise ValueError('Unsafe PubMed XML declaration')
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError('Malformed PubMed XML') from exc
    if root.tag != 'PubmedArticleSet' or root.find('.//ERROR') is not None:
        raise ValueError('Unexpected PubMed XML response')
    return list(root)


def _text(node: ET.Element | None) -> str:
    return ' '.join(''.join(node.itertext()).split()) if node is not None else ''


def _parts_date(parts: list[Any]) -> str | None:
    if not 1 <= len(parts) <= 3 or any(not str(part).isascii() or not str(part).isdecimal() for part in parts):
        return None
    year, month, day = ([int(part) for part in parts] + [1, 1])[:3]
    try:
        checked = date(year, month, day).isoformat()
    except ValueError:
        return None
    return checked[:(4, 7, 10)[len(parts) - 1]]


def _xml_date(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    year, month, day = (node.findtext(tag) for tag in ('Year', 'Month', 'Day'))
    if month and month.lower() in MONTHS:
        month = str(MONTHS.index(month.lower()) + 1)
    if year:
        return _parts_date([value for value in (year, month, day if month else None) if value])
    return None


def pubmed_record(node: ET.Element) -> Row | None:
    citation = node.find('MedlineCitation')
    if citation is None:
        return None
    pmid = citation.findtext('PMID', '')
    title = _text(citation.find('Article/ArticleTitle'))
    if not PMID.fullmatch(pmid) or not title:
        return None
    identifiers = {n.get('IdType', ''): _text(n) for n in node.findall('PubmedData/ArticleIdList/ArticleId')}
    types = [_text(n) for n in citation.findall('Article/PublicationTypeList/PublicationType')]
    kind = next((label for wire, label in (NOTICE_TYPES | KINDS).items() if wire in types), 'unknown')
    dates = {
        'published': _xml_date(citation.find('Article/ArticleDate'))
        or _xml_date(citation.find('Article/Journal/JournalIssue/PubDate')),
        'pubmed_modified': _xml_date(citation.find('DateRevised')),
    }
    history = {'pubmed': 'pubmed_entry', 'entrez': 'pubmed_created', 'medline': 'mesh_history'}
    for event in node.findall('PubmedData/History/PubMedPubDate'):
        if event.get('PubStatus', '') in history:
            dates[history[event.attrib['PubStatus']]] = _xml_date(event)
    authors = []
    for author in citation.findall('Article/AuthorList/Author'):
        name = author.findtext('CollectiveName') or ' '.join(
            value for tag in ('ForeName', 'LastName', 'Suffix') if (value := author.findtext(tag))
        )
        if name:
            authors.append({'name': name, 'affiliations': [_text(n) for n in author.findall('AffiliationInfo/Affiliation')]})
    notices = [
        {key: value for key, value in {
            'type': notice.get('RefType'), 'pmid': notice.findtext('PMID'), 'citation': notice.findtext('RefSource'),
        }.items() if value}
        for notice in citation.findall('CommentsCorrectionsList/CommentsCorrections')
        if (notice.get('RefType') or '').startswith(('Erratum', 'Retraction', 'ExpressionOfConcern', 'Republished', 'Update'))
    ]
    return {
        'pmid': pmid, 'doi': doi(identifiers.get('doi')) or
        doi(citation.findtext('Article/ELocationID[@EIdType="doi"]')), 'pmcid': identifiers.get('pmc'),
        'title': title, 'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/', 'kind': kind, 'source_types': types,
        'journal': citation.findtext('Article/Journal/Title'), 'authors': authors,
        'language': [n.text for n in citation.findall('Article/Language') if n.text],
        'dates': {key: value for key, value in dates.items() if value},
        'mesh_descriptors': [_text(n) for n in citation.findall('MeshHeadingList/MeshHeading/DescriptorName')],
        'keywords': [_text(n) for n in citation.findall('KeywordList/Keyword')],
        'abstract_available': citation.find('Article/Abstract/AbstractText') is not None,
        'notices': notices,
    }


def pubmed(search: Search, start: date, end: date, audit: Counter[str]) -> Iterator[Row]:
    dates = f'{start:%Y/%m/%d}:{end:%Y/%m/%d}'
    term = f'({search.pubmed}) AND (' + ' OR '.join(f'{dates}[{tag}]' for tag in ('crdt', 'mdat', 'mhda')) + ')'
    offset = 0
    seen = set()
    for _ in range(3):
        body = read(PUBMED + 'esearch.fcgi', {
            'db': 'pubmed', 'retmode': 'json', 'retmax': 50, 'retstart': offset, 'term': term,
        }, audit)
        response = json.loads(body)
        result = response.get('esearchresult') if isinstance(response, dict) else None
        if not isinstance(result, dict) or str(result.get('retstart')) != str(offset):
            raise ValueError('Unexpected PubMed search response')
        ids = result.get('idlist')
        count = str(result.get('count'))
        if not isinstance(ids, list) or len(ids) > 50 or not count.isascii() or not count.isdecimal():
            raise ValueError('Invalid PubMed pagination')
        audit['search_pages'] += 1
        warnings = result.get('warninglist')
        if isinstance(warnings, dict):
            warnings = {key: values for key, values in warnings.items() if values}
        if result.get('errorlist') or (
            warnings and not (count == '0' and warnings == {'outputmessages': ['No items found.']})
        ):
            audit['query_warnings'] += 1
        total = int(count)
        if offset > total or (offset < total and not ids):
            raise ValueError('Incomplete PubMed search page')
        if not ids:
            return
        requested = {value for value in ids if isinstance(value, str) and PMID.fullmatch(value)}
        if len(requested) != len(ids) or requested & seen:
            raise ValueError('Invalid or repeated PubMed search identifiers')
        seen.update(requested)
        audit['entries'] += len(ids)
        response = read(PUBMED + 'efetch.fcgi', {'db': 'pubmed', 'id': ','.join(ids), 'retmode': 'xml'}, audit)
        returned = set()
        for node in xml_records(response):
            row = pubmed_record(node)
            if row is None or row['pmid'] not in requested or row['pmid'] in returned:
                audit['invalid'] += 1
                continue
            returned.add(row['pmid'])
            yield row
        audit['missing_records'] += len(requested - returned)
        offset += len(ids)
        if offset >= total:
            return
    audit['page_caps'] += 1


def _terms(value: object) -> set[str]:
    return set(re.findall(r'[a-z0-9]+', fold(value)))


def _matches(phrase: str, value: str) -> bool:
    tokens = _terms(value)
    # A small explicit flexion handles English plurals and psychometric/psychometrics.
    return all(token in tokens or token + 's' in tokens or token.rstrip('s') in tokens
               for token in _terms(phrase) - {'a', 'at', 'da', 'de', 'do', 'of', 'no', 'the'})


def _crossref_date(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    stamp = value.get('date-time')
    if isinstance(stamp, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', stamp):
        try:
            datetime.fromisoformat(stamp)
        except ValueError:
            return None
        return stamp
    dates = value.get('date-parts')
    return _parts_date(dates[0]) if isinstance(dates, list) and dates and isinstance(dates[0], list) else None


def _objects(value: Any) -> Iterator[Row]:
    return (element for element in value if isinstance(element, dict)) if isinstance(value, list) else iter(())


def _first(value: Any) -> str | None:
    return next((element for element in value if isinstance(element, str) and element.strip()), None) if isinstance(value, list) else None


def _licensed_abstract(value: Any, licenses: list[Row]) -> Row:
    if not isinstance(value, str) or not value.strip():
        return {}
    today = datetime.now(timezone.utc).date().isoformat()
    for license in licenses:
        if (license['scope'] != 'vor' or not REUSABLE_LICENSE.fullmatch(license['url'])
                or (license['starts'] and license['starts'][:10] > today)):
            continue
        parser = ListingParser()
        parser.feed(value)
        parser.close()
        text = parser.root.text()
        if text and len(text) <= 5000:
            return {'abstract': text, 'abstract_license': license['url']}
    return {}


def _crossref_record(work: Row, identifier: str, title: str) -> Row:
    dates = {
        'published': ('published-online', 'published', 'published-print', 'issued'),
        'crossref_indexed': ('indexed',), 'crossref_created': ('created',), 'crossref_deposited': ('deposited',),
    }
    relations = work.get('relation')
    linked = [
        {'type': key, 'doi': related_doi}
        for key, entries in relations.items() if key in ('is-preprint-of', 'has-preprint', 'is-version-of', 'has-version')
        for entry in _objects(entries)
        if entry.get('id-type') == 'doi' and (related_doi := doi(entry.get('id')))
    ] if isinstance(relations, dict) else []
    updates = [
        {'direction': direction, 'type': change['type'], 'doi': doi(change.get('DOI')),
         'updated': _crossref_date(change.get('updated'))}
        for direction in ('update-to', 'updated-by')
        for change in _objects(work.get(direction)) if isinstance(change.get('type'), str)
    ]
    wire_type = work.get('type')
    outgoing = {update['type'] for update in updates if update['direction'] == 'update-to'}
    notice_type = next((value for key, value in (
        ('retraction', 'retraction'), ('expression_of_concern', 'expression_of_concern'),
        ('correction', 'correction'), ('erratum', 'correction'), ('corrigendum', 'correction'),
    ) if key in outgoing), None)
    kind = notice_type or ('preprint' if work.get('subtype') == 'preprint' else {
        'journal-article': 'article', 'report': 'technical_report', 'posted-content': 'posted_content',
    }.get(wire_type, 'unknown') if isinstance(wire_type, str) else 'unknown')
    licenses = [
        {'url': item['URL'], 'scope': item.get('content-version'), 'starts': _crossref_date(item.get('start'))}
        for item in _objects(work.get('license'))
        if isinstance(item.get('URL'), str) and urlsplit(item['URL']).scheme in ('https', 'http')
    ]
    resource = work.get('resource')
    primary = resource.get('primary') if isinstance(resource, dict) else None
    url = primary.get('URL') if isinstance(primary, dict) else None
    native_pid = None
    if identifier.startswith('10.1590/') and isinstance(url, str):
        parts = urlsplit(url)
        if parts.hostname in ('www.scielo.br', 'scielo.br') and parts.path == '/scielo.php':
            pid = parse_qs(parts.query).get('pid', [''])[0]
            if re.fullmatch(r'S\d{4}-\d{3}[\dX]\d{13}', pid):
                native_pid = pid
    return {
        'doi': identifier, 'title': title, 'url': 'https://doi.org/' + quote(identifier, safe='/'),
        'kind': kind, 'source_types': [wire_type] if isinstance(wire_type, str) else [],
        'journal': _first(work.get('container-title')), 'publisher': work.get('publisher'),
        'language': [work['language']] if isinstance(work.get('language'), str) else [],
        'authors': [{'name': person.get('name') or ' '.join(
            person[key] for key in ('given', 'family') if isinstance(person.get(key), str)),
            'affiliations': [entry['name'] for entry in _objects(person.get('affiliation'))
                             if isinstance(entry.get('name'), str)]}
            for person in _objects(work.get('author'))],
        'dates': {key: value for key, fields in dates.items()
                  if (value := next((value for field in fields if (value := _crossref_date(work.get(field)))), None))},
        'keywords': [subject for subject in work['subject'] if isinstance(subject, str)]
        if isinstance(work.get('subject'), list) else [],
        'licenses': licenses,
        'abstract_available': isinstance(work.get('abstract'), str) and bool(work['abstract'].strip()),
        **_licensed_abstract(work.get('abstract'), licenses),
        'notices': updates, 'related_works': linked, 'scielo_pid': native_pid,
    }


def crossref(phrase: str, start: date, end: date, audit: Counter[str], *, scielo: bool = False) -> Iterator[Row]:
    filters = f'from-index-date:{start},until-index-date:{end}'
    if scielo:
        filters += ',prefix:10.1590'
    cursor = '*'
    seen = set()
    for _ in range(2):
        response = json.loads(read(CROSSREF, {
            'query.title': phrase, 'filter': filters, 'rows': 20, 'cursor': cursor,
        }, audit))
        message = response.get('message') if isinstance(response, dict) and response.get('status') == 'ok' else None
        if not isinstance(message, dict):
            raise ValueError('Invalid Crossref response')
        items = message.get('items')
        if not isinstance(items, list) or len(items) > 20:
            raise ValueError('Invalid Crossref response')
        audit['search_pages'] += 1
        audit['entries'] += len(items)
        for work in items:
            identifier = doi(work.get('DOI')) if isinstance(work, dict) else None
            title = _first(work.get('title')) if isinstance(work, dict) else None
            if not identifier or not title:
                audit['invalid'] += 1
                continue
            if scielo and not identifier.startswith('10.1590/'):
                audit['outside_collection'] += 1
                continue
            if identifier in seen:
                audit['duplicate_entries'] += 1
                continue
            seen.add(identifier)
            if not _matches(phrase, title):
                audit['outside_topic'] += 1
                continue
            try:
                row = _crossref_record(work, identifier, title)
            except ValueError:
                audit['invalid'] += 1
                continue
            yield row
        if len(items) < 20:
            return
        # Crossref can return the same opaque cursor even when it advanced.
        cursor = message.get('next-cursor')
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2300:
            raise ValueError('Incomplete Crossref cursor page')
    audit['page_caps'] += 1


def scielo_article(crossref_row: Row, audit: Counter[str]) -> Row:
    pid = crossref_row['scielo_pid']
    row = json.loads(read(SCIELO, {'code': pid, 'collection': 'scl', 'format': 'json', 'body': 'false'}, audit))
    if not isinstance(row, dict) or row.get('collection') != 'scl' or row.get('code') != pid:
        raise ValueError('SciELO article identifier mismatch')
    article = row.get('article')
    if not isinstance(article, dict):
        raise ValueError('Invalid SciELO article metadata')
    native_doi = doi(row.get('doi')) or next((doi(item.get('_')) for item in _objects(article.get('v237'))), None)
    if native_doi and native_doi != crossref_row['doi']:
        raise ValueError('SciELO DOI conflict')
    original_title = next((item.get('_') for item in _objects(article.get('v12'))
                           if _terms(item.get('_')) == _terms(crossref_row['title'])), None)
    if not isinstance(original_title, str):
        raise ValueError('SciELO title does not corroborate the deposited article identifier')
    publication = row.get('publication_date')
    processed = row.get('processing_date')
    dates = {key: checked for key, value in (('published', publication), ('scielo_processed', processed))
             if isinstance(value, str) and (checked := _parts_date(value.split('-')))}
    wire_type = row.get('document_type')
    return {
        'official_id': 'scielo:' + pid, 'doi': crossref_row['doi'], 'title': original_title, 'url': crossref_row['url'],
        'dates': dates, 'topics': crossref_row['topics'],
        'kind': {'review-article': 'review', 'research-article': 'article',
                 'correction': 'correction', 'retraction': 'retraction'}.get(wire_type, 'unknown')
        if isinstance(wire_type, str) else 'unknown',
        'source_types': [wire_type] if isinstance(wire_type, str) else [],
        'language': [item['_'] for item in _objects(article.get('v40')) if isinstance(item.get('_'), str)],
        'keywords': list(dict.fromkeys(item['k'] for item in _objects(article.get('v85'))
                                       if isinstance(item.get('k'), str))),
        'abstract_available': any(bool(item.get('a')) for item in _objects(article.get('v83'))),
    }


def openalex(phrase: str, start: date, end: date, audit: Counter[str]) -> Iterator[Row]:
    filters = f'from_publication_date:{start},to_publication_date:{end},title.search:"{phrase}"'
    response = json.loads(read(OPENALEX, {
        'filter': filters, 'per_page': 25,
        'select': 'id,ids,display_name,type,publication_date,language,primary_location,authorships,'
                  'indexed_in,open_access,is_retracted,keywords,abstract_inverted_index,created_date,updated_date',
    }, audit))
    rows = response.get('results') if isinstance(response, dict) else None
    meta = response.get('meta') if isinstance(response, dict) else None
    total = meta.get('count') if isinstance(meta, dict) else None
    if not isinstance(rows, list) or len(rows) > 25 or type(total) is not int or total < len(rows):
        raise ValueError('Invalid OpenAlex response')
    audit['search_pages'] += 1
    audit['entries'] += len(rows)
    if total > len(rows):
        audit['page_caps'] += 1
    for work in rows:
        identifier = work.get('id') if isinstance(work, dict) else None
        title = work.get('display_name') if isinstance(work, dict) else None
        if (not isinstance(identifier, str) or not re.fullmatch(r'https://openalex\.org/W\d+', identifier)
                or not isinstance(title, str) or not title.strip()):
            audit['invalid'] += 1
            continue
        indexed = work.get('indexed_in')
        if not _matches(phrase, title) or (isinstance(indexed, list) and {'crossref', 'pubmed'}.intersection(indexed)):
            audit['outside_topic_or_primary_indexes'] += 1
            continue
        ids = work['ids'] if isinstance(work.get('ids'), dict) else {}
        pmid = re.fullmatch(r'https://pubmed\.ncbi\.nlm\.nih\.gov/([1-9]\d{0,11})/?', str(ids.get('pmid', '')))
        work_doi = doi(ids.get('doi'))
        location = work['primary_location'] if isinstance(work.get('primary_location'), dict) else {}
        journal = location['source'] if isinstance(location.get('source'), dict) else {}
        access = work['open_access'] if isinstance(work.get('open_access'), dict) else {}
        wire_type = work.get('type')
        kind = {'preprint': 'preprint', 'review': 'review', 'report': 'technical_report',
                'conference-abstract': 'conference_abstract'
                }.get(wire_type, 'unknown') if isinstance(wire_type, str) else 'unknown'
        dates = {}
        for label, key in (('published', 'publication_date'), ('openalex_created', 'created_date'),
                           ('openalex_modified', 'updated_date')):
            stamp = work.get(key)
            if isinstance(stamp, str) and (validated := _crossref_date({'date-time': stamp})):
                dates[label] = validated
            elif isinstance(stamp, str) and len(stamp) == 10 and (validated := _parts_date(stamp.split('-'))):
                dates[label] = validated
        yield {
            'official_id': 'openalex:' + identifier.rsplit('/', 1)[-1], 'doi': work_doi, 'pmid': pmid[1] if pmid else None,
            'title': title, 'url': 'https://doi.org/' + quote(work_doi, safe='/') if work_doi else identifier,
            'kind': kind, 'source_types': [wire_type] if isinstance(wire_type, str) else [],
            'journal': journal.get('display_name'), 'language': [work['language']] if work.get('language') else [],
            'dates': dates, 'keywords': [k['display_name'] for k in _objects(work.get('keywords')) if k.get('display_name')],
            'authors': [{'name': author['author']['display_name'], 'affiliations': [
                institution['display_name'] for institution in _objects(author.get('institutions'))
                if institution.get('display_name')
            ]} for author in _objects(work.get('authorships'))
                if isinstance(author.get('author'), dict) and author['author'].get('display_name')],
            'open_access': {key: access[key] for key in ('is_oa', 'oa_status') if key in access},
            'licenses': [{'label': location['license'], 'scope': location.get('version')}] if location.get('license') else [],
            'abstract_available': bool(work.get('abstract_inverted_index')),
            'notices': [{'type': 'retraction_flag'}] if work.get('is_retracted') is True else [],
        }


def radars(audit: Counter[str]) -> Iterator[Row]:
    parser = ListingParser()
    parser.feed(read(RADARS, {}, audit).decode('utf-8-sig'))
    parser.close()
    if parser.root.first('#historico') is None:
        raise ValueError('Missing INPI radar archive')
    for selector, label, date_selector in (
        ('.rad-card', '.rad-card-title', '.rad-card-meta'), ('.hist-item', '.hist-title', '.hist-year'),
    ):
        for element in parser.root.select(selector):
            audit['entries'] += 1
            heading = element.first(label)
            date_node = element.first(date_selector)
            link = next((url for anchor in element.select('a') if (url := anchor.attrs.get('href', ''))
                         and (url == RADAR_ARCHIVE or ('/inpi/' in url and url.lower().endswith('.pdf')))), '')
            title = heading.text() if heading else ''
            timeframe = date_node.text() if date_node else ''
            edition = re.search(r'\bEd\.\s*(\d+)', title + ' ' + timeframe)
            year = re.search(r'\b(20\d{2})\b', timeframe)
            if not edition or not title or not link:
                audit['invalid'] += 1
                continue
            try:
                validate_url(link, {'www.gov.br'})
            except ValueError:
                audit['invalid'] += 1
                continue
            month = re.search(r'\b(' + '|'.join(PT_MONTHS) + r')/20\d{2}', timeframe)
            published = year[0] + (f'-{PT_MONTHS.index(month[0][:3]) + 1:02d}' if month else '') if year else None
            topic = _terms(title)
            if 'aeronautica' in topic or not any(_matches(phrase, title) for phrase in (
                'inteligência artificial', 'tecnologia assistiva', 'transferência de tecnologia',
                'biotecnologia em saúde',
            )):
                audit['outside_topic'] += 1
                continue
            yield {'official_id': f'inpi-radar:{edition[1]}', 'title': title, 'url': link,
                   'catalog_url': RADARS, 'kind': 'technical_report',
                   'dates': {'published': published} if published else {}, 'language': ['pt']}


def _keys(row: Row) -> set[str]:
    return {f'{key}:{row[key]}' for key in ('doi', 'pmid', 'official_id') if row.get(key)}


def _compatible(a: Row, b: Row) -> bool:
    if any(a.get(key) and b.get(key) and a[key] != b[key] for key in ('doi', 'pmid')):
        return False
    left = a.get('official_id') or ''
    right = b.get('official_id') or ''
    if left and right and left != right and left.partition(':')[0] == right.partition(':')[0]:
        return False
    kinds = {a.get('kind'), b.get('kind')} - {None, 'unknown', 'posted_content'}
    return len(kinds) <= 1 or not (kinds & DISTINCT_KINDS)


def _merge(previous: Row, incoming: Row) -> Row:
    merged = previous | {key: value for key, value in incoming.items() if value not in (None, '', [], {})}
    if merged.get('kind') == 'unknown' and previous.get('kind'):
        merged['kind'] = previous['kind']
    merged['dates'] = previous.get('dates', {}) | incoming.get('dates', {})
    for key in ('topics', 'notices', 'related_works'):
        if key in previous or key in incoming:
            merged[key] = list({json.dumps(value, sort_keys=True): value
                                for value in previous.get(key, []) + incoming.get(key, [])}.values())
    return merged


def reconcile(previous: list[Row], observations: Observations) -> list[Row]:
    """Retain last-known metadata by source, including sources absent from this run."""
    records: dict[int, dict[str, Row]] = {}
    by_key: dict[str, set[int]] = {}
    counter = 0
    for source, row in [(source, row) for entry in previous for source, row in entry['observations'].items()] + observations:
        keys = _keys(row)
        candidates = set().union(*(by_key.get(key, set()) for key in keys))
        compatible: list[int] = []
        for candidate in sorted(candidates):
            members = records[candidate].values()
            if all(_compatible(row, member) for member in members) and all(
                _compatible(member, other)
                for index in compatible for member in members for other in records[index].values()
            ):
                compatible.append(candidate)
        if compatible:
            index = compatible[0]
        else:
            index = counter
            counter += 1
            records[index] = {}
        target = records[index]
        for stale in compatible[1:]:
            for provider, member in records.pop(stale).items():
                target[provider] = _merge(member, target.get(provider, {}))
                for key in _keys(member):
                    by_key[key].discard(stale)
                    by_key[key].add(index)
        target[source] = _merge(target.get(source, {}), row)
        for key in keys:
            by_key.setdefault(key, set()).add(index)
    return [{'observations': record} for record in records.values()]


def preview(start: date, end: date, previous: list[Row]) -> Row:
    if not 1 <= (end - start).days + 1 <= MAX_PERIOD:
        raise ValueError('Science replay must span 1 to 31 days')
    if end > datetime.now(timezone.utc).date():
        raise ValueError('Science replay cannot end in the future')
    observations: Observations = []
    sources = {}
    for source in ('pubmed', 'crossref', 'scielo_crossref', 'openalex', 'inpi_radars'):
        audits = {}
        tasks = [('technology_radar', '', None)] if source == 'inpi_radars' else [
            (search.key, language, phrase if source != 'pubmed' else search)
            for search in SEARCHES
            for language, phrases in (
                [('mesh_title_abstract', ('',))] if source == 'pubmed' else
                [('pt', search.portuguese)] if source == 'scielo_crossref' else
                [('en', search.english), ('pt', search.portuguese)]
            ) for phrase in phrases
        ]
        if source == 'openalex':
            tasks = [('neurocognitive_assessment', 'pt', 'avaliação neuropsicológica')]
        for number, (topic, language, query) in enumerate(tasks):
            audit: Counter[str] = Counter()
            entry: Row = {'topic': topic, 'query_language': language}
            try:
                if query is None:
                    rows = radars(audit)
                elif isinstance(query, Search):
                    rows = pubmed(query, start, end, audit)
                elif source == 'openalex':
                    rows = openalex(query, start, end, audit)
                else:
                    rows = crossref(query, start, end, audit, scielo=source == 'scielo_crossref')
                for row in rows:
                    audit['valid'] += 1
                    observations.append((source, row | {'topics': [topic]}))
            except (ValueError, RuntimeError) as exc:
                entry['error'] = str(exc) if isinstance(exc, RuntimeError) else str(exc).splitlines()[0][:150]
            limited = any(audit[key] for key in ('page_caps', 'missing_records', 'invalid', 'query_warnings'))
            entry.update(audit)
            entry['status'] = (
                'partial' if limited or ('error' in entry and audit['valid']) else
                'unavailable' if 'error' in entry else 'success' if audit['valid'] else 'empty'
            )
            audits[str(number + 1)] = entry
        states = {entry['status'] for entry in audits.values()}
        status = (
            'unavailable' if states == {'unavailable'} else 'partial' if states & {'partial', 'unavailable'} else
            'success' if 'success' in states else 'empty'
        )
        source_rows = [(provider, row) for provider, row in observations if provider == source]
        sources[source] = {
            'status': status,
            'queries': audits,
            'distinct_observed': len(reconcile([], source_rows)),
        }
    candidates = {row['doi']: row for source, row in observations if source in ('scielo_crossref', 'crossref')
                  and row.get('scielo_pid')}
    native_audit: Counter[str] = Counter()
    native_status: Row = {'candidates': len(candidates), 'errors': []}
    for candidate in list(candidates.values())[:5]:
        try:
            observations.append(('scielo_articlemeta', scielo_article(candidate, native_audit)))
            native_audit['distinct_observed'] += 1
        except (RuntimeError, ValueError) as exc:
            native_status['errors'].append(str(exc).splitlines()[0][:150])
    native_status.update(native_audit)
    native_status['status'] = (
        'partial' if len(candidates) > 5 or (native_status['errors'] and native_audit['distinct_observed']) else
        'unavailable' if native_status['errors'] else 'success' if candidates else 'empty'
    )
    sources['scielo_articlemeta'] = native_status
    records = reconcile(previous, observations)
    states = {entry['status'] for source, entry in sources.items()
              if source != 'scielo_articlemeta' or candidates}
    return {
        'mode': 'science-preview', 'status': 'failed' if states == {'unavailable'} else
        'partial' if states & {'partial', 'unavailable'} else 'success',
        'from_date': start.isoformat(), 'through_date': end.isoformat(), 'sources': sources,
        'assessment': {'topic_match': 'source_query', 'technical_applicability': 'manual_review',
                       'evidence_quality': 'manual_review'},
        'observed': len(observations), 'distinct_observed': len(reconcile([], observations)),
        'previous': len(previous), 'retained': len(records), 'records': records,
    }


def default_start(end: date) -> date:
    return end - timedelta(days=OVERLAP - 1)


def load_snapshot(path: Path) -> list[Row]:
    with path.open('rb') as snapshot:
        raw = snapshot.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError('Science snapshot exceeds its size limit')
    data = json.loads(raw)
    records = data.get('records') if isinstance(data, dict) and data.get('mode') == 'science-preview' else None
    if not isinstance(records, list):
        raise ValueError('Invalid science snapshot')
    for entry in records:
        observations = entry.get('observations') if isinstance(entry, dict) else None
        if not isinstance(observations, dict) or not observations:
            raise ValueError('Invalid science snapshot observation group')
        for row in observations.values():
            if (not isinstance(row, dict) or not _keys(row) or not isinstance(row.get('dates'), dict)
                    or any(key in row and not isinstance(row[key], list)
                           for key in ('topics', 'notices', 'related_works'))):
                raise ValueError('Invalid science snapshot record')
    return records
