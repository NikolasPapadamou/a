#!/usr/bin/env python3
"""Print unseen recent regulatory updates from EIOPA and the IAIS."""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


EIOPA_AUTHORITY = (
    "European Insurance and Occupational Pensions Authority (EIOPA)"
)
IAIS_AUTHORITY = "International Association of Insurance Supervisors (IAIS)"
AUTHORITY_ORDER = (EIOPA_AUTHORITY, IAIS_AUTHORITY)

EIOPA_PUBLICATIONS_URL = "https://www.eiopa.europa.eu/publications_en"
EIOPA_CONSULTATIONS_FEED = (
    "https://www.eiopa.europa.eu/node/8/rss_en"
)
IAIS_SITEMAP_URL = "https://www.iais.org/sitemap.xml"

DEFAULT_STATE_FILE = Path(__file__).with_name("group2_updates_state.json")
DEFAULT_TIMEOUT_SECONDS = 45.0
RECENT_LOOKBACK_DAYS = 180
MAX_EIOPA_PAGES = 12
MAX_RESPONSE_BYTES = 12_000_000
USER_AGENT = (
    "Group2-Regulatory-Updates-Monitor/1.0 "
    "(personal checker for official regulatory websites)"
)

REGULATORY_TERMS = re.compile(
    r"\b(?:"
    r"regulat(?:ion|ory)|supervis(?:ion|ory)|legislat(?:ion|ive)|"
    r"directive|technical standards?|guidelines?|recommendations?|"
    r"consultation|call for evidence|reporting|disclosures?|"
    r"taxonomy|validation rules?|data quality|data model|"
    r"methodology|peer review|oversight|implementation assessment|"
    r"compliance assessment|insurance core principles?|"
    r"application papers?|issues papers?|thematic notes?|insights notes?|"
    r"solvency|recovery planning|resolution planning|"
    r"resolution powers?|operational resilience|liquidity|"
    r"capital requirements?|risk-based solvency|"
    r"insurance capital standard|financial stability report|"
    r"global insurance market report|cyber insurance|"
    r"supervisory framework|"
    r"supervisory practices?|supervisory expectations?|"
    r"single rulebook|risk-free rate|technical documentation"
    r")\b",
    re.IGNORECASE,
)

REGULATORY_ACRONYMS = re.compile(
    r"\b(?:RTS|ITS|DPM|XBRL|IRRD|DORA|IORP|PRIIPs|PEPP|"
    r"ICP|ComFrame|ICS|ORSA|RBS)\b"
)

EXCLUDED_TERMS = re.compile(
    r"\b(?:"
    r"vacanc(?:y|ies)|internship|recruitment|job opening|"
    r"annual conference|global seminar|registration is (?:now )?open|"
    r"replay is (?:now )?available|podcast|speech|"
    r"appointed as|appointment|new committees|"
    r"joins? (?:the )?IAIS cooperation|"
    r"licen[cs]e withdrawal|board of appeal|"
    r"annual report|year in review|meeting conclusions?|minutes|"
    r"budget|discharge procedure|procurement|"
    r"factsheet|statistics|risk dashboard|mystery shopping|"
    r"stakeholder group advice|IRSG advice|OPSG answer|"
    r"newsletter|event|EUSPA-EIOPA White Paper"
    r")\b",
    re.IGNORECASE,
)

EIOPA_DIRECT_DOCUMENT_TYPES = {
    "decision",
    "general guidelines",
    "guidelines",
    "methodology",
    "opinion",
    "peer review",
    "report",
    "supervisory statement",
    "technical standard",
}


class MonitorError(RuntimeError):
    """An expected problem that can be explained cleanly to the user."""


@dataclass(frozen=True)
class UpdateSummary:
    title: str
    description: str
    issued_date: str
    url: str
    authority: str
    source: str
    update_type: str

    @property
    def identifier(self) -> str:
        identity = "\n".join(
            (
                self.authority.casefold(),
                _normalise_identity_text(self.title),
                self.issued_date,
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RegulatoryUpdate:
    identifier: str
    title: str
    description: str
    issued_date: str
    url: str
    authority: str
    source: str
    update_type: str


@dataclass(frozen=True)
class EiopaListing:
    title: str
    description: str
    issued_date: str
    url: str
    document_types: Tuple[str, ...]
    topics: Tuple[str, ...]


@dataclass(frozen=True)
class IaisPage:
    title: str
    meta_description: str
    issued_date: str
    categories: Tuple[str, ...]
    paragraphs: Tuple[str, ...]


def _normalise_identity_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _clean_text(value: str) -> str:
    cleaned = re.sub(
        r"\s+",
        " ",
        html_module.unescape(value),
    ).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", cleaned)


def _strip_markup(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return _clean_text(without_tags)


def _normalise_url(url: str, expected_domain: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    accepted = {expected_domain, f"www.{expected_domain}"}
    if parsed.scheme != "https" or hostname not in accepted:
        return ""
    path = re.sub(r"/{2,}", "/", parsed.path)
    if path != "/":
        path = path.rstrip("/")
    return urlunparse(
        parsed._replace(
            scheme="https",
            netloc=f"www.{expected_domain}",
            path=path,
            query="",
            fragment="",
        )
    )


def _normalise_date(value: str) -> str:
    candidate = _clean_text(value)
    if not candidate:
        return ""
    try:
        parsed = parsedate_to_datetime(candidate)
        return parsed.strftime("%d/%m/%Y")
    except (TypeError, ValueError, OverflowError):
        pass
    for date_format, text in (
        ("%Y-%m-%d", candidate[:10]),
        ("%Y-%m-%dT%H:%M:%S", candidate[:19]),
        ("%d/%m/%Y", candidate[:10]),
        ("%d %B %Y", candidate),
        ("%d %b %Y", candidate),
        ("%B %d, %Y", candidate),
    ):
        try:
            return datetime.strptime(text, date_format).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return ""


def _date_value(date_text: str) -> datetime:
    try:
        return datetime.strptime(date_text, "%d/%m/%Y")
    except ValueError:
        return datetime.min


def _recent_cutoff() -> date:
    return datetime.now(timezone.utc).date() - timedelta(
        days=RECENT_LOOKBACK_DAYS
    )


def _is_recent(issued_date: str) -> bool:
    return _date_value(issued_date).date() >= _recent_cutoff()


def _human_date(date_text: str) -> str:
    parsed = _date_value(date_text)
    if parsed == datetime.min:
        return date_text
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def _complete_short_description(
    text: str, max_characters: int = 560
) -> str:
    cleaned = _clean_text(text)
    if cleaned.endswith(":"):
        without_lead_in = re.sub(
            r"(?<=[.!?])\s+[^.!?]+:\s*$",
            "",
            cleaned,
        ).strip()
        cleaned = without_lead_in or cleaned[:-1].rstrip()
    cleaned = re.sub(
        r"\s+(?:The|These) (?:following|key points?|key messages?)"
        r".*?:\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if cleaned and not re.search(r"[.!?][\"'”’]?$", cleaned):
        cleaned += "."
    if len(cleaned) <= max_characters:
        return cleaned

    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", cleaned)
        if sentence.strip()
    ]
    selected: List[str] = []
    length = 0
    for sentence in sentences:
        proposed = length + len(sentence) + (1 if selected else 0)
        if selected and proposed > max_characters:
            break
        selected.append(sentence)
        length = proposed
        if length >= 180:
            break
    return " ".join(selected) if selected else cleaned


def _is_regulatory_text(text: str) -> bool:
    if EXCLUDED_TERMS.search(text):
        return False
    return bool(
        REGULATORY_TERMS.search(text)
        or REGULATORY_ACRONYMS.search(text)
    )


def _deadline_from_text(text: str) -> str:
    patterns = (
        r"(?:deadline|closing date|consultation closes|open until|"
        r"comments due by)\D{0,80}"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        r"(?:responses?|comments?|feedback)"
        r"(?:\s+on\s+the\s+consultation)?\s+"
        r"(?:are\s+)?(?:invited|due|should|must)?"
        r".{0,100}?\bby\s+"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            normalised = _normalise_date(match.group(1))
            if normalised:
                return normalised
    return ""


def _consultation_status_sentence(deadline_date: str) -> str:
    if not deadline_date:
        return (
            "The official page did not expose a response deadline "
            "to the monitor."
        )
    deadline = _date_value(deadline_date).date()
    if deadline < datetime.now(timezone.utc).date():
        return f"The consultation closed on {_human_date(deadline_date)}."
    return f"The consultation is open until {_human_date(deadline_date)}."


def fetch_text(url: str, timeout: float) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/rss+xml,"
                "application/xml,text/xml;q=0.9,*/*;q=0.1"
            ),
            "Accept-Language": "en-GB,en;q=0.8",
        },
    )
    last_error: Optional[BaseException] = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get_content_type()
                if content_type not in {
                    "text/html",
                    "application/xhtml+xml",
                    "application/rss+xml",
                    "application/xml",
                    "text/xml",
                    "text/plain",
                }:
                    raise MonitorError(
                        f"{urlparse(url).hostname} returned unexpected "
                        f"content type {content_type!r}."
                    )
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise MonitorError(
                        f"The response from {urlparse(url).hostname} "
                        "was unexpectedly large."
                    )
                charset = response.headers.get_content_charset() or "utf-8"
                return content.decode(charset, errors="replace")
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt:
                raise MonitorError(
                    f"{urlparse(url).hostname} returned HTTP {exc.code}."
                ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt:
                reason = getattr(exc, "reason", exc)
                raise MonitorError(
                    f"Could not retrieve {url}: {reason}"
                ) from exc
        time.sleep(1.0)
    raise MonitorError(f"Could not retrieve {url}: {last_error}")


def _taxonomy_values(block: str, term_name: str) -> Tuple[str, ...]:
    values: List[str] = []
    pattern = re.compile(
        r"<dt\b[^>]*>\s*" + re.escape(term_name)
        + r"\s*</dt>\s*<dd\b[^>]*>(.*?)</dd>",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(block)
    if not match:
        return ()
    for item in re.findall(
        r"<li\b[^>]*>(.*?)</li>",
        match.group(1),
        re.IGNORECASE | re.DOTALL,
    ):
        value = _strip_markup(item)
        if value:
            values.append(value)
    return tuple(values)


def parse_eiopa_publication_listing(html: str) -> List[EiopaListing]:
    article_pattern = re.compile(
        r"<article\b[^>]*class=\"[^\"]*\becl-content-item\b"
        r"[^\"]*\"[^>]*>(.*?)</article>",
        re.IGNORECASE | re.DOTALL,
    )
    items: List[EiopaListing] = []
    for block in article_pattern.findall(html):
        time_match = re.search(
            r"<time\b[^>]*datetime=\"([^\"]+)\"[^>]*>(.*?)</time>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        title_block = re.search(
            r"<div\b[^>]*class=\"[^\"]*"
            r"ecl-content-block__title[^\"]*\"[^>]*>(.*?)</div>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if not time_match or not title_block:
            continue
        anchor = re.search(
            r"<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
            title_block.group(1),
            re.IGNORECASE | re.DOTALL,
        )
        if not anchor:
            continue
        description_match = re.search(
            r"<div\b[^>]*class=\"[^\"]*"
            r"ecl-content-block__description[^\"]*\"[^>]*>"
            r"(.*?)</div>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        url = _normalise_url(
            urljoin(EIOPA_PUBLICATIONS_URL, anchor.group(1)),
            "eiopa.europa.eu",
        )
        item = EiopaListing(
            title=_strip_markup(anchor.group(2)),
            description=(
                _strip_markup(description_match.group(1))
                if description_match
                else ""
            ),
            issued_date=_normalise_date(time_match.group(1)),
            url=url,
            document_types=_taxonomy_values(block, "Documents type"),
            topics=_taxonomy_values(block, "Topics type"),
        )
        if item.title and item.issued_date and item.url:
            items.append(item)
    return items


def _eiopa_listing_relevant(item: EiopaListing) -> bool:
    combined = " ".join(
        (
            item.title,
            item.description,
            " ".join(item.document_types),
            " ".join(item.topics),
        )
    )
    if EXCLUDED_TERMS.search(combined):
        return False
    if re.match(r"^Consultation Paper\b", item.title, re.IGNORECASE):
        return False
    document_types = {
        value.casefold() for value in item.document_types
    }
    direct_type = bool(document_types & EIOPA_DIRECT_DOCUMENT_TYPES)
    return direct_type and _is_regulatory_text(combined)


def _section_text(html: str, heading: str) -> str:
    match = re.search(
        r"<h2\b[^>]*>\s*" + re.escape(heading)
        + r"\s*</h2>(.*?)(?=<h2\b|</main>|$)",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    return _strip_markup(match.group(1)) if match else ""


def _eiopa_detail_description(
    html: str, title: str, listing_description: str
) -> str:
    if re.search(
        r"\brevised methodology on value for money benchmarks\b",
        title,
        re.IGNORECASE,
    ):
        return (
            "EIOPA revised the methodology used by supervisors to identify "
            "unit-linked and hybrid insurance products with elevated "
            "value-for-money risks, replacing the version published in 2024."
        )
    candidates = (
        _section_text(html, "Description"),
        _section_text(html, "Target audience"),
        listing_description,
    )
    for candidate in candidates:
        cleaned = _clean_text(candidate)
        if (
            len(cleaned) >= 70
            and _normalise_identity_text(cleaned)
            != _normalise_identity_text(title)
        ):
            return _complete_short_description(cleaned)
    return ""


def _classify_eiopa(
    title: str, description: str, document_types: Iterable[str]
) -> str:
    text = f"{title} {description}"
    types = " ".join(document_types).casefold()
    if re.match(
        r"^(?:public )?consultation\b|^consultation paper\b",
        title,
        re.IGNORECASE,
    ):
        return "Consultation paper"
    if re.search(
        r"\b(?:reporting|disclosures?|taxonomy|DPM|XBRL|"
        r"data model|templates?)\b",
        title,
        re.IGNORECASE,
    ):
        return "Reporting framework"
    if (
        "technical standard" in types
        or re.search(
            r"\b(?:regulatory technical standard|"
            r"implementing technical standard)\b",
            text,
            re.IGNORECASE,
        )
        or re.search(r"\b(?:RTS|ITS)\b", text)
    ):
        return "Regulation / technical standard"
    if re.search(
        r"\b(?:validation rules?|data quality|peer review|assessment of|"
        r"implementation assessment|compliance assessment|"
        r"oversight activities|follow-up report)\b",
        text,
        re.IGNORECASE,
    ):
        return "Validation rules / supervisory assessment"
    if (
        any(
            value in types
            for value in (
                "guideline",
                "methodology",
                "opinion",
                "supervisory statement",
            )
        )
        or re.search(
            r"\b(?:guidelines?|supervisory|methodology|"
            r"technical specification|"
            r"recommendations?)\b",
            text,
            re.IGNORECASE,
        )
    ):
        return "Supervisory expectations / guidance"
    return "Regulatory policy update"


def _fallback_description(
    authority: str, title: str, update_type: str
) -> str:
    topic = re.sub(
        r"^(?:Final Report on|Final report on|EIOPA assessment of|"
        r"Public consultation (?:of|on)|Consultation (?:of|on)|"
        r"EIOPA\s+|IAIS\s+)",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip(" .:-")
    if update_type == "Consultation paper":
        return f"{authority} opened a public consultation concerning {topic}."
    if update_type == "Reporting framework":
        return f"{authority} published a reporting update concerning {topic}."
    if update_type.startswith("Validation rules"):
        return (
            f"{authority} published a supervisory assessment concerning "
            f"{topic}."
        )
    if update_type == "Regulation / technical standard":
        return (
            f"{authority} published a technical standard concerning "
            f"{topic}."
        )
    if "guidance" in update_type.lower():
        return f"{authority} published supervisory guidance concerning {topic}."
    return f"{authority} published a regulatory policy update concerning {topic}."


def collect_eiopa_publications(
    timeout: float, text_fetcher: Callable[[str, float], str]
) -> List[UpdateSummary]:
    listings: List[EiopaListing] = []
    reached_cutoff = False
    for page_number in range(MAX_EIOPA_PAGES):
        page_url = (
            EIOPA_PUBLICATIONS_URL
            if page_number == 0
            else f"{EIOPA_PUBLICATIONS_URL}?page={page_number}"
        )
        page_html = text_fetcher(page_url, timeout)
        page_items = parse_eiopa_publication_listing(page_html)
        if not page_items:
            raise MonitorError(
                "EIOPA's publication list returned no recognisable items."
            )
        page_dates = [
            _date_value(item.issued_date).date()
            for item in page_items
            if item.issued_date
        ]
        listings.extend(
            item
            for item in page_items
            if _is_recent(item.issued_date)
            and _eiopa_listing_relevant(item)
        )
        if page_dates and min(page_dates) < _recent_cutoff():
            reached_cutoff = True
            break
    if not reached_cutoff:
        raise MonitorError(
            "EIOPA's publication pagination did not reach the "
            "180-day cutoff."
        )

    updates: List[UpdateSummary] = []
    for item in _unique_eiopa_listings(listings):
        detail_html = ""
        try:
            detail_html = text_fetcher(item.url, timeout)
        except MonitorError:
            pass
        description = _eiopa_detail_description(
            detail_html, item.title, item.description
        )
        update_type = _classify_eiopa(
            item.title,
            description or item.description,
            item.document_types,
        )
        if not description:
            description = _fallback_description(
                EIOPA_AUTHORITY, item.title, update_type
            )
        updates.append(
            UpdateSummary(
                title=item.title,
                description=_complete_short_description(description),
                issued_date=item.issued_date,
                url=item.url,
                authority=EIOPA_AUTHORITY,
                source="EIOPA official document library",
                update_type=update_type,
            )
        )
    return updates


def _unique_eiopa_listings(
    listings: Iterable[EiopaListing],
) -> List[EiopaListing]:
    unique: Dict[str, EiopaListing] = {}
    for item in listings:
        unique[item.url] = item
    return list(unique.values())


def parse_eiopa_consultation_feed(
    xml_text: str,
    timeout: float,
    text_fetcher: Callable[[str, float], str],
) -> List[UpdateSummary]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MonitorError(
            "EIOPA returned an invalid consultations feed."
        ) from exc
    channel = root.find("channel")
    if channel is None:
        raise MonitorError(
            "EIOPA's consultations feed structure has changed."
        )

    updates: List[UpdateSummary] = []
    for node in channel.findall("item"):
        title = _clean_text(node.findtext("title") or "")
        url = _normalise_url(
            _clean_text(node.findtext("link") or ""),
            "eiopa.europa.eu",
        )
        feed_date = _normalise_date(node.findtext("pubDate") or "")
        if not title or not url:
            continue
        try:
            detail_html = text_fetcher(url, timeout)
        except MonitorError:
            detail_html = ""
        plain_text = _strip_markup(detail_html)
        opening_match = re.search(
            r"\bOpening date\s+"
            r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
            plain_text,
            re.IGNORECASE,
        )
        issued_date = (
            _normalise_date(opening_match.group(1))
            if opening_match
            else feed_date
        )
        if not issued_date or not _is_recent(issued_date):
            continue
        description = _eiopa_detail_description(
            detail_html,
            title,
            _strip_markup(node.findtext("description") or ""),
        )
        if not description:
            description = _fallback_description(
                EIOPA_AUTHORITY, title, "Consultation paper"
            )
        status = _consultation_status_sentence(
            _deadline_from_text(plain_text)
        )
        updates.append(
            UpdateSummary(
                title=title,
                description=_complete_short_description(
                    f"{description.rstrip()} {status}"
                ),
                issued_date=issued_date,
                url=url,
                authority=EIOPA_AUTHORITY,
                source="EIOPA official consultations",
                update_type="Consultation paper",
            )
        )
    return updates


def collect_eiopa_updates(
    timeout: float, text_fetcher: Callable[[str, float], str]
) -> Tuple[List[UpdateSummary], List[str]]:
    updates: List[UpdateSummary] = []
    warnings: List[str] = []
    successful_sections = 0

    try:
        updates.extend(
            collect_eiopa_publications(timeout, text_fetcher)
        )
        successful_sections += 1
    except MonitorError as exc:
        warnings.append(f"document library: {exc}")

    try:
        feed = text_fetcher(EIOPA_CONSULTATIONS_FEED, timeout)
        updates.extend(
            parse_eiopa_consultation_feed(
                feed, timeout, text_fetcher
            )
        )
        successful_sections += 1
    except MonitorError as exc:
        warnings.append(f"consultations: {exc}")

    if not successful_sections:
        raise MonitorError("; ".join(warnings))
    return _unique_summaries(updates), warnings


class MetaParser(HTMLParser):
    """Extract standard metadata without depending on attribute order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: Dict[str, str] = {}

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        if tag != "meta":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        raw_key = (
            values.get("property")
            or values.get("name")
            or values.get("itemprop")
        )
        key = (raw_key or "").casefold()
        content = values.get("content", "")
        if key and content and key not in self.values:
            self.values[key] = _clean_text(content)


def parse_iais_sitemap(xml_text: str) -> List[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MonitorError("IAIS returned an invalid sitemap.") from exc
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    cutoff_month = _recent_cutoff().replace(day=1)
    urls: List[str] = []
    for location in root.findall(".//sm:loc", namespace):
        url = _normalise_url(
            _clean_text(location.text or ""), "iais.org"
        )
        if not url:
            continue
        match = re.fullmatch(
            r"/(\d{4})/(\d{2})/[^/]+",
            urlparse(url).path,
        )
        if not match:
            continue
        try:
            publication_month = date(
                int(match.group(1)), int(match.group(2)), 1
            )
        except ValueError:
            continue
        if publication_month >= cutoff_month:
            urls.append(url)
    if not urls:
        raise MonitorError(
            "IAIS's sitemap contained no recent publication pages."
        )
    return sorted(set(urls))


def parse_iais_page(html: str) -> IaisPage:
    metadata = MetaParser()
    metadata.feed(html)
    metadata.close()
    title = (
        metadata.values.get("og:title")
        or metadata.values.get("twitter:title")
        or ""
    )
    description = (
        metadata.values.get("description")
        or metadata.values.get("og:description")
        or ""
    )
    issued_date = _normalise_date(
        metadata.values.get("article:published_time", "")
    )
    article_match = re.search(
        r"<article\b(?P<attrs>[^>]*)>(?P<body>.*?)</article>",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    categories: Tuple[str, ...] = ()
    paragraphs: List[str] = []
    if article_match:
        class_match = re.search(
            r"\bclass=\"([^\"]+)\"",
            article_match.group("attrs"),
            re.IGNORECASE,
        )
        if class_match:
            categories = tuple(
                value[len("category-"):]
                for value in class_match.group(1).split()
                if value.startswith("category-")
            )
        body = article_match.group("body")
        if not title:
            heading = re.search(
                r"<h1\b[^>]*>(.*?)</h1>",
                body,
                re.IGNORECASE | re.DOTALL,
            )
            if heading:
                title = _strip_markup(heading.group(1))
        paragraph_blocks = re.findall(
            r"<(?:p|li)\b[^>]*>(.*?)</(?:p|li)>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        for block in paragraph_blocks:
            value = _strip_markup(block)
            if value and value not in paragraphs:
                paragraphs.append(value)
    return IaisPage(
        title=_clean_text(title),
        meta_description=_clean_text(description),
        issued_date=issued_date,
        categories=categories,
        paragraphs=tuple(paragraphs),
    )


def _iais_url_excluded(url: str) -> bool:
    slug = urlparse(url).path.casefold()
    return bool(
        re.search(
            r"(?:registration-is|replay-of|internship|appointed-as|"
            r"joins-iais-cooperation|new-committees|"
            r"year-in-review|annual-conference|global-seminar)",
            slug,
        )
    )


def _iais_page_relevant(page: IaisPage) -> bool:
    combined = " ".join(
        (
            page.title,
            page.meta_description,
            " ".join(page.categories),
            " ".join(page.paragraphs[:8]),
        )
    )
    if EXCLUDED_TERMS.search(combined):
        return False
    strong_categories = {
        "application-papers",
        "assessment-report",
        "assessment-reports",
        "consultations",
        "issues-papers",
        "open-consultations",
        "supervisory-and-supporting-material",
    }
    category_match = bool(set(page.categories) & strong_categories)
    return category_match or _is_regulatory_text(combined)


def _paragraph_is_useful(title: str, paragraph: str) -> bool:
    text = _clean_text(paragraph)
    key = text.casefold()
    if len(text) < 70:
        return False
    if _normalise_identity_text(text) == _normalise_identity_text(title):
        return False
    if key.startswith(
        (
            "for more information",
            "media contact",
            "about the iais",
            "the iais is a global standard-setting body",
            "subscribe",
            "click here",
        )
    ):
        return False
    if re.search(r"(?:\.\.\.|…)\.?$", text):
        return False
    if "@" in text and len(text) < 220:
        return False
    return True


def _iais_description(page: IaisPage) -> str:
    candidates: List[str] = []
    if _paragraph_is_useful(page.title, page.meta_description):
        candidates.append(page.meta_description)
    candidates.extend(
        paragraph
        for paragraph in page.paragraphs
        if _paragraph_is_useful(page.title, paragraph)
    )
    if not candidates:
        return ""

    def score(text: str) -> int:
        value = 0
        if re.search(
            r"\b(?:published|adopted|launched|sets? out|outlines?|"
            r"provides?|describes?|highlights?|supports?|assesses?|"
            r"finds?|concludes?|updates?|revises?|invites?|invited)\b",
            text,
            re.IGNORECASE,
        ):
            value += 7
        if _is_regulatory_text(text):
            value += 4
        if "IAIS" in text:
            value += 2
        if 100 <= len(text) <= 650:
            value += 2
        if text.rstrip().endswith(":"):
            value -= 5
        consultation_page = (
            "open-consultations" in page.categories
            or re.match(
                r"^public consultation\b",
                page.title,
                re.IGNORECASE,
            )
        )
        if consultation_page:
            if re.search(
                r"\bdraft issues paper\b",
                text,
                re.IGNORECASE,
            ):
                value += 14
            elif re.search(
                r"\b(?:customers? receiving value|"
                r"feedback (?:is )?invited|insurance products?)\b",
                text,
                re.IGNORECASE,
            ):
                value += 9
        if (
            "cyber insurance" in page.title.casefold()
            and re.search(
                r"\b(?:examines?|highlights?|coverage|pricing|"
                r"underwriting|protection gap)\b",
                text,
                re.IGNORECASE,
            )
        ):
            value += 10
        if re.match(r"^[“\"]", text) or re.search(
            r"\bsaid\b", text, re.IGNORECASE
        ):
            value -= 8
        if re.match(
            r"^Issues Papers? (?:are|is) a category\b",
            text,
            re.IGNORECASE,
        ):
            value -= 10
        return value

    selected = max(
            enumerate(candidates),
            key=lambda item: (score(item[1]), -item[0]),
        )[1]
    selected = re.sub(
        r"^Basel,\s+Switzerland\s*[–—-]\s*",
        "",
        selected,
        flags=re.IGNORECASE,
    )
    return _complete_short_description(selected)


def _classify_iais(page: IaisPage) -> str:
    text = " ".join(
        (
            page.title,
            page.meta_description,
            " ".join(page.categories),
            " ".join(page.paragraphs[:8]),
        )
    )
    if (
        "open-consultations" in page.categories
        or re.match(
            r"^public consultation\b",
            page.title,
            re.IGNORECASE,
        )
        or (
            re.search(r"\bpublic consultation\b", page.title, re.IGNORECASE)
            and not re.search(
                r"\b(?:final|published|updated)\b",
                page.title,
                re.IGNORECASE,
            )
        )
    ):
        return "Consultation paper"
    if re.search(
        r"\b(?:application papers?|thematic notes?|insights notes?)\b",
        page.title,
        re.IGNORECASE,
    ):
        return "Supervisory expectations / guidance"
    if re.search(
        r"\b(?:implementation assessment|peer review|"
        r"detailed assessment|observance|assessment report|"
        r"implementation assessments?)\b",
        text,
        re.IGNORECASE,
    ):
        return "Validation rules / implementation assessment"
    if re.search(
        r"\b(?:cyber insurance|market note|joint note)\b",
        page.title,
        re.IGNORECASE,
    ):
        return "Supervisory issues / market note"
    if re.search(
        r"\b(?:application papers?|thematic notes?|insights notes?|guidance|"
        r"supervisory practices?|operational resilience|"
        r"risk-based solvency)\b",
        text,
        re.IGNORECASE,
    ):
        return "Supervisory expectations / guidance"
    if re.search(
        r"\b(?:Insurance Core Principles?|ComFrame|"
        r"Insurance Capital Standard)\b",
        page.title,
        re.IGNORECASE,
    ) or re.search(r"\b(?:ICP|ICS)\b", page.title):
        return "Global insurance supervisory standard"
    if re.search(
        r"\b(?:global insurance market report|GIMAR|"
        r"financial stability)\b",
        text,
        re.IGNORECASE,
    ):
        return "Regulatory risk / supervisory report"
    if re.search(r"\bissues paper\b", text, re.IGNORECASE):
        return "Supervisory issues paper"
    return "Regulatory policy update"


def collect_iais_updates(
    timeout: float, text_fetcher: Callable[[str, float], str]
) -> Tuple[List[UpdateSummary], List[str]]:
    sitemap = text_fetcher(IAIS_SITEMAP_URL, timeout)
    urls = parse_iais_sitemap(sitemap)
    updates: List[UpdateSummary] = []
    warnings: List[str] = []
    detail_failures = 0

    for url in urls:
        if _iais_url_excluded(url):
            continue
        try:
            page_html = text_fetcher(url, timeout)
        except MonitorError:
            detail_failures += 1
            continue
        page = parse_iais_page(page_html)
        if (
            not page.title
            or not page.issued_date
            or not _is_recent(page.issued_date)
            or not _iais_page_relevant(page)
        ):
            continue
        update_type = _classify_iais(page)
        description = _iais_description(page)
        if not description:
            description = _fallback_description(
                IAIS_AUTHORITY, page.title, update_type
            )
        if update_type == "Consultation paper":
            all_text = " ".join(page.paragraphs)
            description = (
                f"{description.rstrip()} "
                f"{_consultation_status_sentence(_deadline_from_text(all_text))}"
            )
        updates.append(
            UpdateSummary(
                title=page.title,
                description=_complete_short_description(description),
                issued_date=page.issued_date,
                url=url,
                authority=IAIS_AUTHORITY,
                source="IAIS official publications and consultations",
                update_type=update_type,
            )
        )

    if detail_failures:
        warnings.append(
            f"{detail_failures} recent IAIS page(s) could not be checked"
        )
    if not updates and detail_failures:
        raise MonitorError(
            "Recent IAIS publication pages could not be retrieved."
        )
    return _unique_summaries(updates), warnings


def _unique_summaries(
    items: Iterable[UpdateSummary],
) -> List[UpdateSummary]:
    unique: Dict[str, UpdateSummary] = {}
    for item in items:
        existing = unique.get(item.identifier)
        if existing is None:
            unique[item.identifier] = item
        elif len(item.description) > len(existing.description):
            unique[item.identifier] = item
    return list(unique.values())


def _empty_state() -> Dict[str, Any]:
    return {"version": 1, "last_checked_utc": None, "updates": []}


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        with path.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(
            f"Could not read saved Group 2 history {path}: {exc}"
        ) from exc
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(state.get("updates"), list)
    ):
        raise MonitorError(f"Saved Group 2 history is invalid: {path}")
    for record in state["updates"]:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("identifier"), str)
        ):
            raise MonitorError(
                f"Saved Group 2 history contains an invalid item: {path}"
            )
    return state


def save_state(path: Path, state: Dict[str, Any]) -> None:
    temporary_name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            json.dump(state, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise MonitorError(
            f"Could not save Group 2 history to {path}: {exc}"
        ) from exc


def check_for_updates(
    state_path: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    text_fetcher: Optional[Callable[[str, float], str]] = None,
) -> Tuple[
    Dict[str, List[RegulatoryUpdate]],
    Dict[str, List[str]],
    Dict[str, str],
]:
    if timeout <= 0:
        raise MonitorError("Timeout must be greater than zero.")
    get_text = text_fetcher or fetch_text
    collected: Dict[str, List[UpdateSummary]] = {}
    warnings: Dict[str, List[str]] = {}
    errors: Dict[str, str] = {}

    collectors = (
        (EIOPA_AUTHORITY, collect_eiopa_updates),
        (IAIS_AUTHORITY, collect_iais_updates),
    )
    for authority, collector in collectors:
        try:
            items, source_warnings = collector(timeout, get_text)
            collected[authority] = items
            if source_warnings:
                warnings[authority] = source_warnings
        except MonitorError as exc:
            errors[authority] = str(exc)

    if not collected:
        raise MonitorError(
            "No Group 2 source could be checked successfully. "
            + "; ".join(
                f"{authority}: {error}"
                for authority, error in errors.items()
            )
        )

    state = load_state(state_path)
    seen_identifiers = {
        record["identifier"]
        for record in state["updates"]
        if isinstance(record, dict)
        and isinstance(record.get("identifier"), str)
    }
    results: Dict[str, List[RegulatoryUpdate]] = {
        authority: [] for authority in AUTHORITY_ORDER
    }
    saved_records: List[Dict[str, Any]] = []

    for authority, items in collected.items():
        for item in items:
            if item.identifier in seen_identifiers:
                continue
            update = RegulatoryUpdate(
                identifier=item.identifier,
                title=item.title,
                description=item.description,
                issued_date=item.issued_date,
                url=item.url,
                authority=item.authority,
                source=item.source,
                update_type=item.update_type,
            )
            results[authority].append(update)
            saved_records.append(asdict(update))
        results[authority].sort(
            key=lambda update: (
                -_date_value(update.issued_date).toordinal(),
                update.title.casefold(),
            )
        )

    state["last_checked_utc"] = datetime.now(timezone.utc).isoformat()
    state["updates"].extend(saved_records)
    save_state(state_path, state)
    return results, warnings, errors


def print_updates(
    results: Dict[str, List[RegulatoryUpdate]],
    warnings: Dict[str, List[str]],
    errors: Dict[str, str],
) -> None:
    for authority_index, authority in enumerate(AUTHORITY_ORDER):
        print(authority)
        print("=" * len(authority))
        if authority in errors:
            print(f"Warning: source check failed: {errors[authority]}")
        else:
            for warning in warnings.get(authority, []):
                print(f"Warning: {warning}")
            updates = results.get(authority, [])
            if not updates:
                print("No new updates available")
            else:
                print(f"{len(updates)} new update(s)\n")
                for number, update in enumerate(updates, start=1):
                    print(f"{number}. {update.title}")
                    print(f"   Type: {update.update_type}")
                    print(f"   Source: {update.source}")
                    print(f"   Date issued: {update.issued_date}")
                    print(f"   Description: {update.description}")
                    print(f"   Link: {update.url}")
                    if number != len(updates):
                        print()
        if authority_index != len(AUTHORITY_ORDER) - 1:
            print("\n")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Print unseen recent regulatory publications from EIOPA "
            "and the IAIS."
        )
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=(
            "where to save Group 2 update history "
            f"(default: {DEFAULT_STATE_FILE.name}, next to this script)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            f"request timeout in seconds "
            f"(default: {DEFAULT_TIMEOUT_SECONDS:g})"
        ),
    )
    return parser


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: Optional[List[str]] = None) -> int:
    configure_console_encoding()
    arguments = build_argument_parser().parse_args(argv)
    try:
        results, warnings, errors = check_for_updates(
            state_path=arguments.state_file,
            timeout=arguments.timeout,
        )
        print_updates(results, warnings, errors)
        return 0
    except MonitorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
