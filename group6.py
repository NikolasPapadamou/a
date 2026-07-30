#!/usr/bin/env python3
"""Print unseen regulatory updates from the official Group 6 sources."""

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

# Group 6 deliberately shares the already-tested official FATF, FSB and
# DG FISMA adapters with Groups 4 and 5. The files are kept together, so these
# imports work when this script is run directly from the fca_monitor folder.
import group4 as group4_monitor
import group5 as group5_monitor


ESRB_PUBLICATIONS_TEMPLATE = (
    "https://www.esrb.europa.eu/pub/pubbydate/{year}/html/"
    "index_include.en.html"
)
ESRB_RECOMMENDATIONS_URL = (
    "https://www.esrb.europa.eu/mppa/recommendations/html/index.en.html"
)
ESRB_WARNINGS_URL = (
    "https://www.esrb.europa.eu/mppa/warnings/html/index.en.html"
)
AMLA_RESOURCES_URL = "https://www.amla.europa.eu/resources_en"
AMLA_CONSULTATIONS_URL = (
    "https://www.amla.europa.eu/policy/public-consultations_en"
)
FATF_PUBLICATIONS_URL = group5_monitor.FATF_RESULTS_URL
FSB_HOME_URL = group4_monitor.FSB_HOME_URL
ESMA_LIBRARY_URL = (
    "https://www.esma.europa.eu/databases-library/esma-library"
    "?items_per_page=100"
)
FISMA_CONSULTATIONS_URL = group4_monitor.FISMA_CONSULTATIONS_URL
FISMA_PUBLICATIONS_URL = group4_monitor.FISMA_PUBLICATIONS_URL
FISMA_HOME_URL = group4_monitor.FISMA_HOME_URL
EURLEX_SPARQL_URL = "https://publications.europa.eu/webapi/rdf/sparql"

ESRB_AUTHORITY = "European Systemic Risk Board (ESRB)"
AMLA_AUTHORITY = (
    "Authority for Anti-Money Laundering and Countering the Financing "
    "of Terrorism (AMLA)"
)
FATF_AUTHORITY = group5_monitor.FATF_AUTHORITY
FSB_AUTHORITY = group4_monitor.FSB_AUTHORITY
ESMA_AUTHORITY = "European Securities and Markets Authority (ESMA)"
FISMA_AUTHORITY = group4_monitor.FISMA_AUTHORITY
EURLEX_AUTHORITY = "EUR-Lex"
AUTHORITY_ORDER = (
    ESRB_AUTHORITY,
    AMLA_AUTHORITY,
    FATF_AUTHORITY,
    FSB_AUTHORITY,
    ESMA_AUTHORITY,
    FISMA_AUTHORITY,
    EURLEX_AUTHORITY,
)

DEFAULT_STATE_FILE = Path(__file__).with_name("group6_updates_state.json")
DEFAULT_TIMEOUT_SECONDS = 45.0
MAX_RESPONSE_BYTES = 12_000_000
MAX_DETAIL_WORKERS = 8
MAX_ESRB_ITEMS = 24
MAX_AMLA_ITEMS = 24
MAX_ESMA_ITEMS = 30
MAX_EURLEX_ITEMS = 30
MAX_FATF_PUBLICATIONS = 8
MAX_FATF_CONSULTATIONS = 5
RECENT_LOOKBACK_DAYS = 180
USER_AGENT = (
    "Group6-Regulatory-Updates-Monitor/1.0 "
    "(personal checker for official regulatory websites)"
)

REGULATORY_TERMS = re.compile(
    r"\b(?:"
    r"regulat(?:ion|ory)|rules?|requirements?|directive|standards?|"
    r"consultation|consultative|exposure draft|"
    r"supervis(?:ion|ory)|expectations?|guidance|guidelines?|"
    r"recommendation|warning|macroprudential|"
    r"validation|pre-validation|data quality|assessment|methodology|"
    r"reporting|reporting framework|disclosure|taxonomy|templates?|"
    r"implementation|monitoring|compliance|frequently asked questions|"
    r"prudential|capital|liquidity|margin|market risk|operational risk|"
    r"financial stability|payment|settlement|cross-border|"
    r"AML|CFT|money laundering|terrorist financing|"
    r"crypto(?:-| )assets?|securities|investment firms?|ESG ratings?|"
    r"risk management|technical advice|technical standards?"
    r")\b",
    re.IGNORECASE,
)


class MonitorError(RuntimeError):
    """An expected problem that can be explained cleanly to the user."""


@dataclass(frozen=True)
class UpdateSummary:
    title: str
    url: str
    authority: str
    source: str
    update_type: str
    issued_date: str
    description: str = ""

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


def _normalise_identity_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _strip_markup(text: str) -> str:
    no_markup = re.sub(r"<[^>]+>", " ", text)
    return re.sub(
        r"\s+", " ", html_module.unescape(no_markup)
    ).strip()


def _shorten_text(text: str, max_characters: int = 420) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_characters:
        return cleaned
    shortened = cleaned[: max_characters + 1]
    sentence_ends = [
        shortened.rfind(marker)
        for marker in (". ", "? ", "! ")
    ]
    sentence_end = max(sentence_ends)
    if sentence_end >= max_characters // 2:
        return shortened[: sentence_end + 1].strip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,;:") + "..."


def _normalise_date(date_text: str) -> str:
    return group5_monitor._normalise_date(date_text)


def _classify_update(title: str, description: str, source: str) -> str:
    original_text = " ".join((title, description, source))
    text = " ".join((title, description, source)).casefold()
    title_key = title.casefold()
    source_key = source.casefold()
    consultation_response = bool(
        re.search(
            r"\b(?:response|responses|feedback)\s+(?:to|on)\b.*"
            r"\bconsultation\b|overview of consultation responses",
            title_key,
        )
    )
    if consultation_response:
        return "Regulatory or policy update"
    if (
        title_key.startswith(
            (
                "consultation",
                "public consultation",
                "targeted consultation",
            )
        )
        or "consultation paper" in title_key
        or "consultative document" in title_key
        or "exposure draft" in title_key
        or (
            "consultation" in source_key
            and "response" not in title_key
        )
    ):
        return "Consultation paper"
    if (
        "validation" in text
        or "data quality" in text
        or "risk assessment" in text
        or "assessment methodology" in text
    ):
        return "Validation rules / assessment"
    if (
        "reporting framework" in text
        or "reporting requirement" in text
        or "reporting package" in text
        or "reporting template" in text
        or "taxonomy" in text
        or "disclosure" in text
        or re.search(r"\b(?:reporting|templates?)\b", text)
    ):
        return "Reporting framework"
    if (
        "supervisory" in text
        or "expectation" in text
        or "guideline" in text
        or "recommendation" in text
        or "warning" in text
        or "compliance table" in text
    ):
        return "Supervisory expectations / guidance"
    if (
        "regulatory technical standard" in text
        or "implementing technical standard" in text
        or re.search(r"\b(?:RTS|ITS)\b", original_text)
    ):
        return "Regulation / standard-setting update"
    return group5_monitor._classify_update(title, description, source)


def _recent_cutoff() -> date:
    return datetime.now(timezone.utc).date() - timedelta(
        days=RECENT_LOOKBACK_DAYS
    )


def _is_recent(item: UpdateSummary) -> bool:
    return _date_value(item.issued_date).date() >= _recent_cutoff()


def _human_date(date_text: str) -> str:
    parsed = _date_value(date_text)
    if parsed == datetime.min:
        return date_text
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def _consultation_status_sentence(
    deadline_date: str,
    stated_status: str = "",
) -> str:
    if not deadline_date:
        return ""
    deadline = _date_value(deadline_date).date()
    status = stated_status.casefold()
    if status == "closed" or deadline < datetime.now(timezone.utc).date():
        return f"The consultation closed on {_human_date(deadline_date)}."
    return f"The consultation is open until {_human_date(deadline_date)}."


def _deadline_from_html(page_html: str) -> str:
    decoded = html_module.unescape(html_module.unescape(page_html))
    definition_deadline = re.search(
        r"<dt\b[^>]*>\s*Deadline\s*</dt>.*?"
        r"<time\b[^>]*datetime=[\"'](?P<date>[^\"']+)[\"']",
        decoded,
        re.IGNORECASE | re.DOTALL,
    )
    if definition_deadline:
        normalised = _normalise_date(
            definition_deadline.group("date")
        )
        if normalised:
            return normalised

    plain_text = _strip_markup(decoded)
    deadline_patterns = (
        r"(?:responses?|comments?|feedback)\s+(?:should|must)?\s*"
        r"(?:be\s+)?submitted.*?\bby\s+(?:[A-Za-z]+,\s+)?"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        r"(?:please\s+)?send\s+(?:us\s+)?(?:your\s+)?response.*?\bby\s+"
        r"(?:[A-Za-z]+\s+)?(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        r"(?:deadline|open until|closes? on|extend(?:ed)? until)\D{0,70}"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    )
    for pattern in deadline_patterns:
        match = re.search(pattern, plain_text, re.IGNORECASE)
        if match:
            normalised = _normalise_date(match.group(1))
            if normalised:
                return normalised
    return ""


def _normalise_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/{2,}", "/", parsed.path)
    if path != "/":
        path = path.rstrip("/")
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=path,
            fragment="",
        )
    )


def _safe_url(
    base_url: str,
    href: str,
    allowed_hosts: Set[str],
) -> Optional[str]:
    absolute = urljoin(base_url, html_module.unescape(href))
    parsed = urlparse(absolute)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() not in allowed_hosts
    ):
        return None
    return _normalise_url(absolute)


def _fetch_urllib(
    request: Request,
    url: str,
    timeout: float,
    allowed_content_types: Set[str],
) -> str:
    last_error: Optional[BaseException] = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get_content_type()
                if content_type not in allowed_content_types:
                    raise MonitorError(
                        f"Unexpected content type from {url}: {content_type}"
                    )
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise MonitorError(
                        f"Response from {url} was unexpectedly large."
                    )
                charset = response.headers.get_content_charset() or "utf-8"
                return content.decode(charset, errors="replace")
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt:
                raise MonitorError(
                    f"Website returned HTTP {exc.code} while requesting {url}"
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


def fetch_text(url: str, timeout: float) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        },
    )
    return _fetch_urllib(
        request,
        url,
        timeout,
        {"text/html", "application/xhtml+xml"},
    )


def fetch_json(url: str, timeout: float) -> Any:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/sparql-results+json,"
                "application/json;q=0.9,*/*;q=0.1"
            ),
        },
    )
    text = _fetch_urllib(
        request,
        url,
        timeout,
        {
            "application/sparql-results+json",
            "application/json",
            "text/json",
            "text/plain",
        },
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Official feed returned invalid JSON: {url}") from exc


def fetch_fatf_text(url: str, timeout: float) -> str:
    """Fetch a FATF detail page through the same protection layer as its feed."""

    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise MonitorError(
            "FATF requires the curl_cffi package. Run this once in the "
            "Group 6 folder: python -m pip install -r requirements-group6.txt"
        ) from exc

    last_error = ""
    for impersonation in ("chrome124", "safari"):
        try:
            response = curl_requests.get(
                url,
                timeout=timeout,
                impersonate=impersonation,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": "https://www.fatf-gafi.org/en/publications.html",
                },
            )
            if response.status_code == 200:
                encoded = response.content
                if len(encoded) > MAX_RESPONSE_BYTES:
                    raise MonitorError(
                        "The FATF detail response was unexpectedly large."
                    )
                return response.text
            last_error = f"HTTP {response.status_code}"
        except MonitorError:
            raise
        except Exception as exc:
            last_error = str(exc)
    raise MonitorError(f"Could not retrieve the FATF detail page: {last_error}")


def _shared_call(function: Callable[..., Any], *args: Any) -> Any:
    try:
        return function(*args)
    except (group4_monitor.MonitorError, group5_monitor.MonitorError) as exc:
        message = str(exc).replace("Group 5", "Group 6")
        message = message.replace(
            "requirements-group5.txt", "requirements-group6.txt"
        )
        raise MonitorError(message) from exc


def _convert_shared_item(item: Any) -> UpdateSummary:
    return UpdateSummary(
        title=item.title,
        url=item.url,
        authority=item.authority,
        source=item.source,
        update_type=item.update_type,
        issued_date=item.issued_date,
        description=item.description,
    )


def _date_value(date_text: str) -> datetime:
    try:
        return datetime.strptime(date_text, "%d/%m/%Y")
    except ValueError:
        return datetime.min


def _esrb_description(title: str, category: str) -> str:
    topic = re.sub(
        r"^(?:Recommendation|Warning) of the European Systemic Risk Board "
        r"of \d{1,2} [A-Za-z]+ \d{4} (?:on|regarding)\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    topic = re.sub(
        r"\s*\(ESRB/\d{4}/\d+\)\s*$", "", topic, flags=re.IGNORECASE
    ).rstrip(".")
    category_key = category.casefold()
    if "compliance" in title.casefold():
        return (
            "This ESRB compliance report assesses implementation of "
            f"{topic}."
        )
    if "material third countries" in title.casefold():
        return (
            "The ESRB identifies third countries considered material for "
            "the EU banking sector under its macroprudential monitoring "
            "framework."
        )
    if title.casefold().startswith("recommendation"):
        return (
            "The ESRB issued a macroprudential recommendation concerning "
            f"{topic}."
        )
    if title.casefold().startswith("warning"):
        return (
            "The ESRB issued a financial-stability warning concerning "
            f"{topic}."
        )
    if "risk dashboard" in title.casefold():
        return (
            "Summarises the ESRB's latest assessment of systemic risks and "
            "vulnerabilities across the EU financial system, using current "
            "market, banking and macro-financial indicators."
        )
    if "commentar" in category_key:
        if "voluntary reciprocity" in title.casefold():
            return (
                "Reviews ten years of voluntary reciprocity, under which "
                "countries mirror macroprudential measures introduced by "
                "other authorities, and draws lessons for consistent "
                "cross-border implementation."
            )
        return (
            "Explains the macroprudential policy implications of "
            f"{topic.rstrip('.')}."
        )
    title_key = title.casefold()
    if "frontier ai models" in title_key:
        return (
            "Assesses how frontier AI models with advanced cyber capabilities "
            "could create systemic risks for the financial sector and why "
            "authorities may need a coordinated macroprudential response."
        )
    if "buffer usability" in title_key:
        return (
            "Examines obstacles that may prevent banks from using capital "
            "buffers during periods of stress and considers ways to make the "
            "prudential buffer framework more usable."
        )
    if "linkages between banks" in title_key:
        return (
            "Assesses contagion and concentration risks created by financial "
            "links between banks and non-bank financial intermediaries."
        )
    if "geoeconomic fragmentation" in title_key:
        return (
            "Assesses how geopolitical tensions and fragmentation of trade "
            "and financial flows could affect EU financial stability."
        )
    return (
        f"Examines {topic.rstrip('.')} and explains the implications for "
        "financial stability and macroprudential policy."
    )


def _parse_esrb_dated_list(
    page_html: str,
    page_url: str,
    source: str,
) -> List[UpdateSummary]:
    items: List[UpdateSummary] = []
    entry_pattern = re.compile(
        r"<dt\b[^>]*\bisoDate=[\"'](?P<date>[^\"']+)[\"'][^>]*>"
        r".*?</dt>\s*<dd\b[^>]*>(?P<body>.*?)</dd>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in entry_pattern.finditer(page_html):
        body = match.group("body")
        title_match = re.search(
            r"<div\b[^>]*class=[\"'][^\"']*\btitle\b[^\"']*[\"'][^>]*>"
            r".*?<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>"
            r"(?P<title>.*?)</a>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        if not title_match:
            continue
        category_match = re.search(
            r"<div\b[^>]*class=[\"'][^\"']*\bcategory\b[^\"']*[\"'][^>]*>"
            r"(?P<category>.*?)</div>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        title = _strip_markup(title_match.group("title"))
        category = (
            _strip_markup(category_match.group("category"))
            if category_match
            else source
        )
        issued_date = _normalise_date(match.group("date"))
        url = _safe_url(
            page_url,
            title_match.group("href"),
            {"www.esrb.europa.eu", "esrb.europa.eu"},
        )
        if not title or not issued_date or not url:
            continue
        display_source = (
            source
            if category.casefold() == source.casefold()
            else f"{source} — {category}"
        )
        items.append(
            UpdateSummary(
                title=title,
                url=url,
                authority=ESRB_AUTHORITY,
                source=display_source,
                update_type=_classify_update(
                    title, _esrb_description(title, category), source
                ),
                issued_date=issued_date,
                description=_esrb_description(title, category),
            )
        )
    return items


def parse_esrb_sources(
    publications_html: str,
    recommendations_html: str,
    warnings_html: str,
    publications_url: str,
) -> List[UpdateSummary]:
    publications = _parse_esrb_dated_list(
        publications_html, publications_url, "ESRB publications"
    )
    publications = [
        item
        for item in publications
        if any(
            category in item.source.casefold()
            for category in ("reports", "risk dashboard", "commentaries")
        )
        and "annex" not in item.title.casefold()
    ]

    recommendations = _parse_esrb_dated_list(
        recommendations_html,
        ESRB_RECOMMENDATIONS_URL,
        "ESRB recommendations",
    )
    recommendations = [
        item
        for item in recommendations
        if (
            item.title.casefold().startswith("recommendation")
            or "compliance report" in item.title.casefold()
            or "material third countries" in item.title.casefold()
        )
    ]

    warnings = _parse_esrb_dated_list(
        warnings_html, ESRB_WARNINGS_URL, "ESRB warnings"
    )
    warnings = [
        item
        for item in warnings
        if item.title.casefold().startswith("warning")
    ]

    all_items = _unique_items(publications + recommendations + warnings)
    all_items = [item for item in all_items if _is_recent(item)]
    all_items.sort(
        key=lambda item: (-_date_value(item.issued_date).toordinal(), item.title)
    )
    if not all_items:
        raise MonitorError(
            "Could not find dated ESRB reports, recommendations or warnings. "
            "The official pages may have changed."
        )
    return all_items[:MAX_ESRB_ITEMS]


def _parse_ecl_cards(
    page_html: str,
    page_url: str,
    source: str,
    consultation: bool,
) -> List[UpdateSummary]:
    items: List[UpdateSummary] = []
    article_pattern = re.compile(
        r"<article\b[^>]*class=[\"'][^\"']*"
        r"(?:ecl-card|ecl-content-item)[^\"']*[\"'][^>]*>"
        r"(?P<body>.*?)</article>",
        re.IGNORECASE | re.DOTALL,
    )
    for article_match in article_pattern.finditer(page_html):
        body = article_match.group("body")
        title_match = re.search(
            r"(?:ecl-content-block__title|ecl-content-item__title).*?"
            r"<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>"
            r"(?P<title>.*?)</a>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        time_matches = list(re.finditer(
            r"<time\b[^>]*datetime=[\"'](?P<date>[^\"']+)[\"'][^>]*>",
            body,
            re.IGNORECASE,
        ))
        if not title_match or not time_matches:
            continue
        description_match = re.search(
            r"(?:ecl-content-block__description|"
            r"ecl-content-item__description)[^>]*>(?P<description>.*?)"
            r"</(?:div|p)>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        title = _strip_markup(title_match.group("title"))
        description = (
            _strip_markup(description_match.group("description"))
            if description_match
            else ""
        )
        issued_date = _normalise_date(time_matches[0].group("date"))
        url = _safe_url(
            page_url,
            title_match.group("href"),
            {"www.amla.europa.eu", "amla.europa.eu"},
        )
        if not title or not issued_date or not url:
            continue
        if consultation:
            topic = _topic_after_prefix(title)
            status_match = re.search(
                r"\bStatus:\s*(Open|Closed)\b",
                _strip_markup(body),
                re.IGNORECASE,
            )
            stated_status = (
                status_match.group(1) if status_match else ""
            )
            deadline_date = (
                _normalise_date(time_matches[1].group("date"))
                if len(time_matches) > 1
                else ""
            )
            status_sentence = _consultation_status_sentence(
                deadline_date, stated_status
            )
            description = (
                f"AMLA is seeking stakeholder feedback on {topic}. "
                f"{status_sentence}"
            ).strip()
        items.append(
            UpdateSummary(
                title=title,
                url=url,
                authority=AMLA_AUTHORITY,
                source=source,
                update_type=(
                    "Consultation paper"
                    if consultation
                    else _classify_update(title, description, source)
                ),
                issued_date=issued_date,
                description=description,
            )
        )
    return items


def _amla_resource_is_relevant(item: UpdateSummary) -> bool:
    title = item.title.casefold()
    if "consult" in title:
        # The consultation register is monitored separately and is the more
        # substantive official record.
        return False
    if re.search(
        r"\b(?:annual activity report|chair presents|appointment|"
        r"vacancy|event|speech|hearing)\b",
        title,
    ):
        return False
    return bool(REGULATORY_TERMS.search(f"{item.title} {item.description}"))


def parse_amla_sources(
    resources_html: str, consultations_html: str
) -> List[UpdateSummary]:
    resources = [
        item
        for item in _parse_ecl_cards(
            resources_html,
            AMLA_RESOURCES_URL,
            "AMLA resources and media",
            consultation=False,
        )
        if _amla_resource_is_relevant(item)
    ]
    consultations = _parse_ecl_cards(
        consultations_html,
        AMLA_CONSULTATIONS_URL,
        "AMLA public consultations",
        consultation=True,
    )
    items = _unique_items(resources + consultations)
    items.sort(
        key=lambda item: (-_date_value(item.issued_date).toordinal(), item.title)
    )
    if not items:
        raise MonitorError(
            "Could not find AMLA regulatory resources or consultations. "
            "The official pages may have changed."
        )
    return items[:MAX_AMLA_ITEMS]


def _extract_table_cell(row_html: str, class_name: str) -> str:
    match = re.search(
        r"<td\b[^>]*class=[\"'][^\"']*\b"
        + re.escape(class_name)
        + r"\b[^\"']*[\"'][^>]*>(?P<value>.*?)</td>",
        row_html,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group("value") if match else ""


def _esma_item_is_relevant(document_type: str, title: str) -> bool:
    type_key = document_type.casefold()
    title_key = title.casefold()
    if "press release" in type_key:
        return False
    if any(
        allowed in type_key
        for allowed in (
            "consultation",
            "final report",
            "guideline",
            "recommendation",
            "compliance table",
            "q&a",
            "technical advice",
            "technical standard",
        )
    ):
        return True
    if "opinion" in type_key or "decision" in type_key:
        return bool(REGULATORY_TERMS.search(title))
    if "statement" in type_key:
        return bool(REGULATORY_TERMS.search(title))
    if "reference" in type_key:
        return bool(
            re.search(
                r"\b(?:reporting|taxonomy|validation|register|technical "
                r"standard|supervisory|compliance|guideline|framework|"
                r"template|data contributors?)\b",
                title_key,
            )
        ) and not bool(
            re.search(r"\b(?:calendar|contact points?|agenda)\b", title_key)
        )
    return False


def _topic_after_prefix(title: str) -> str:
    topic = re.sub(
        r"^(?:final report|consultation(?: paper)?|public statement|statement|"
        r"compliance table|technical advice|guidelines?|recommendations?|"
        r"questions and answers|q&a)\s+(?:on|regarding|concerning)\s+(?:the\s+)?",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return topic.rstrip(".")


def _clean_esma_title(title: str, url: str) -> str:
    if (
        "public-statement-publication-or-distribution-esg-ratings-third-parties"
        in url
    ):
        return (
            "Public statement on third-party publication or distribution of "
            "ESG ratings during the authorisation transition period"
        )
    return title


def _esma_description(title: str, document_type: str) -> str:
    topic = _topic_after_prefix(title)
    type_key = document_type.casefold()
    title_key = title.casefold()
    if "consultation" in type_key:
        return (
            f"ESMA published a consultation paper seeking stakeholder "
            f"feedback on {topic} before finalising its policy or technical "
            "advice. The ESMA library entry does not state the response "
            "deadline."
        )
    if "compliance table" in type_key:
        return (
            "Shows whether national competent authorities comply, or intend "
            f"to comply, with {topic}."
        )
    if "guideline" in type_key or "recommendation" in type_key:
        return (
            f"Sets out the common supervisory approach and expected "
            f"practices for {topic}."
        )
    if "q&a" in type_key:
        return (
            f"Clarifies how firms and supervisors should apply the relevant "
            f"rules in practice for {topic}."
        )
    if "technical advice" in type_key:
        return (
            f"Provides ESMA's technical analysis and recommendations to "
            f"support EU policy decisions on {topic}."
        )
    if "technical standard" in type_key:
        return (
            f"Specifies the detailed regulatory or implementing requirements "
            f"that apply to {topic}."
        )
    if "final report" in type_key:
        return (
            f"Sets out ESMA's final conclusions and resulting regulatory "
            f"proposals on {topic}."
        )
    if "reference" in type_key:
        if title_key == "csd register":
            return (
                "Lists central securities depositories authorised in the EU "
                "or recognised from third countries, providing a current "
                "reference for their regulatory status."
            )
        if "data contributors" in title_key:
            return (
                "Identifies the entities currently expected to contribute "
                "equity market data to the consolidated tape provider."
            )
        if "escpr" in title_key and "reporting" in title_key:
            return (
                "Provides the applicable reference material for miscellaneous "
                "reports that crowdfunding service providers submit to ESMA "
                "under the EU crowdfunding regime."
            )
        if "notifications of compliance" in title_key:
            return (
                "Consolidates national authorities' notifications on whether "
                "they comply or intend to comply with ESMA guidelines."
            )
        if "eltif" in title_key and "register" in title_key:
            return (
                "Lists European long-term investment funds authorised across "
                "the EU and provides their current registration details."
            )
        if "supervisory briefing" in title_key:
            return (
                "Explains the common supervisory approach to triangular "
                "passporting arrangements and the risks authorities should "
                "consider."
            )
        if "cost-benefit analysis" in title_key:
            return (
                "Compares options for simplifying transaction reporting under "
                "MiFIR, EMIR and SFTR and evaluates their expected costs and "
                "benefits."
            )
        if title_key == "transaction reporting":
            return (
                "Brings together ESMA's current rules, technical resources and "
                "guidance for transaction reporting."
            )
        if "dq framework" in title_key:
            return (
                "Sets out the data-quality checks and assessment method ESMA "
                "uses for MiFIR Article 26 transaction reports."
            )
        if "assessment framework" in title_key and "esrs" in title_key:
            return (
                "Sets out how ESMA will assess and form opinions on technical "
                "advice concerning European Sustainability Reporting "
                "Standards."
            )
        if "register" in title_key:
            return (
                f"Provides the current official register for {topic}, "
                "including the regulatory information ESMA makes available."
            )
        if "reporting" in title_key or "framework" in title_key:
            return (
                f"Provides the technical framework, instructions and "
                f"supporting material used for {topic}."
            )
        return (
            f"Provides the current technical and supervisory reference "
            f"material needed to understand or implement {topic}."
        )
    if "opinion" in type_key:
        return f"Sets out ESMA's supervisory position and reasoning on {topic}."
    if "significant benchmark" in title_key:
        benchmark = re.sub(
            r"^.*?benchmark regulation\s*[-–]\s*",
            "",
            title,
            flags=re.IGNORECASE,
        )
        return (
            "Records the notification of "
            f"{benchmark} as a significant benchmark under Article 24(2) "
            "of the EU Benchmark Regulation."
        )
    if "deprioritisation of supervisory actions" in title_key:
        return (
            "Explains the circumstances in which supervisors will temporarily "
            "deprioritise enforcement concerning mechanically issued invoices "
            "under the reasonable-commercial-basis technical standard."
        )
    if "moody" in title_key and "impose fines" in title_key:
        return (
            "Records ESMA's enforcement decision imposing fines on Moody's "
            "Deutschland for identified regulatory infringements."
        )
    if "esg ratings" in title_key and "transition period" in title_key:
        return (
            "Clarifies how third parties may publish or distribute ESG ratings "
            "during the transition to authorisation, recognition or other "
            "approval under the new EU regime."
        )
    return (
        f"Explains ESMA's regulatory or supervisory position on {topic} and "
        "what affected firms or authorities should take into account."
    )


def parse_esma_library(page_html: str) -> List[UpdateSummary]:
    items: List[UpdateSummary] = []
    for row_match in re.finditer(
        r"<tr\b[^>]*>(?P<row>.*?)</tr>",
        page_html,
        re.IGNORECASE | re.DOTALL,
    ):
        row = row_match.group("row")
        title_cell = _extract_table_cell(row, "views-field-title")
        if not title_cell:
            continue
        title_match = re.search(
            r"<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>"
            r"(?P<title>.*?)</a>",
            title_cell,
            re.IGNORECASE | re.DOTALL,
        )
        time_match = re.search(
            r"<time\b[^>]*datetime=[\"'](?P<date>[^\"']+)[\"'][^>]*>",
            row,
            re.IGNORECASE,
        )
        if not title_match or not time_match:
            continue
        title = _strip_markup(title_match.group("title"))
        document_type = _strip_markup(
            _extract_table_cell(row, "views-field-field-document-type")
        )
        issued_date = _normalise_date(time_match.group("date"))
        url = _safe_url(
            ESMA_LIBRARY_URL,
            title_match.group("href"),
            {"www.esma.europa.eu", "esma.europa.eu"},
        )
        if url:
            title = _clean_esma_title(title, url)
        if (
            not title
            or not document_type
            or not issued_date
            or not url
            or not _esma_item_is_relevant(document_type, title)
        ):
            continue
        description = _esma_description(title, document_type)
        items.append(
            UpdateSummary(
                title=title,
                url=url,
                authority=ESMA_AUTHORITY,
                source=f"ESMA Library — {document_type}",
                update_type=_classify_update(
                    title, description, document_type
                ),
                issued_date=issued_date,
                description=description,
            )
        )
    items = _unique_items(items)
    items.sort(
        key=lambda item: (-_date_value(item.issued_date).toordinal(), item.title)
    )
    if not items:
        raise MonitorError(
            "Could not find relevant dated documents in the ESMA Library. "
            "The official page may have changed."
        )
    return items[:MAX_ESMA_ITEMS]


def _eurlex_start_date(state: Dict[str, Any]) -> date:
    last_checked = state.get("last_checked_utc")
    if isinstance(last_checked, str):
        try:
            checked = datetime.fromisoformat(
                last_checked.replace("Z", "+00:00")
            ).date()
            return checked - timedelta(days=2)
        except ValueError:
            pass
    return datetime.now(timezone.utc).date() - timedelta(days=60)


def _build_eurlex_query_url(start_date: date) -> str:
    query = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT DISTINCT ?celex ?date ?rtype ?title WHERE {{
  ?work cdm:official-journal-act_date_publication ?date ;
        cdm:resource_legal_id_celex ?celex ;
        cdm:work_has_resource-type ?rtype ;
        cdm:resource_legal_responsibility_of_agent
          <http://publications.europa.eu/resource/authority/corporate-body/FISMA> .
  ?expression cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language
          <http://publications.europa.eu/resource/authority/language/ENG> ;
        cdm:expression_title ?title .
  FILTER(?date >= "{start_date.isoformat()}"^^xsd:date)
}}
ORDER BY DESC(?date)
LIMIT 250
""".strip()
    return f"{EURLEX_SPARQL_URL}?{urlencode({'query': query, 'format': 'application/sparql-results+json'})}"


def _eurlex_type_name(resource_type: str) -> str:
    return {
        "REG": "Regulation",
        "REG_DEL": "Delegated regulation",
        "REG_IMPL": "Implementing regulation",
        "DIR": "Directive",
        "DIR_DEL": "Delegated directive",
        "DIR_IMPL": "Implementing directive",
        "DEC": "Decision",
        "DEC_DEL": "Delegated decision",
        "DEC_IMPL": "Implementing decision",
        "RECO": "Recommendation",
    }.get(resource_type, resource_type.replace("_", " ").title())


def _eurlex_plain_summary(title: str) -> str:
    text = title.casefold()
    if "elements of esg rating products" in text:
        return (
            "Specifies which features of ESG rating products providers must "
            "disclose to the public, users, rated entities and issuers."
        )
    if "safeguards" in text and "separate their esg rating activities" in text:
        return (
            "Sets organisational safeguards that ESG rating providers must "
            "use to separate ratings from their other business activities."
        )
    if "third country branches reporting" in text:
        return (
            "Defines the standard supervisory reports that third-country bank "
            "branches must submit under the Capital Requirements Directive."
        )
    if "order execution policies of investment firms" in text:
        return (
            "Defines the criteria investment firms must use to establish and "
            "assess whether their order-execution policies are effective."
        )
    if "residential property under construction" in text:
        return (
            "Defines the equivalent legal safeguards needed for unfinished "
            "residential property to receive the relevant prudential treatment."
        )
    if "permission for trading during closed periods" in text:
        return (
            "Updates market-abuse rules on trading during closed periods, "
            "cross-border trading venues and indicators of manipulation."
        )
    if "disclosure of inside information in protracted processes" in text:
        return (
            "Clarifies when issuers must disclose inside information during "
            "protracted processes and when delayed disclosure is permitted."
        )
    if "alignment of terminology" in text and "regulation (eu) no 575/2013" in text:
        return (
            "Updates existing technical standards to reflect amendments to "
            "the Capital Requirements Regulation and align their terminology."
        )
    if "systems, resources and procedures of external reviewers" in text:
        return (
            "Sets standards for assessing external reviewers' systems, "
            "resources, compliance functions and methodologies, including "
            "applications from third-country reviewers."
        )
    if "notification of material changes" in text and "external reviewer" in text:
        return (
            "Introduces standard forms, templates and procedures for external "
            "reviewers to notify material changes to registration information."
        )
    if "follow-on prospectus" in text and "growth issuance prospectus" in text:
        return (
            "Standardises and reduces the required content of EU follow-on and "
            "EU growth issuance prospectuses."
        )
    if "suspension of the trading obligation for derivatives" in text:
        return (
            "Temporarily suspends the relevant derivatives trading obligation "
            "under the Markets in Financial Instruments Regulation."
        )
    if "format of insider lists" in text:
        return (
            "Introduces an updated standard format for insider lists under the "
            "Market Abuse Regulation."
        )
    if "calculation of the contributions of certain institutions" in text:
        return (
            "Changes how certain institutions' resolution-fund contributions "
            "are calculated and updates related risk indicators and procedures."
        )
    if "third-party execution and research services" in text:
        return (
            "Changes the conditions under which investment firms may obtain "
            "third-party execution and research services."
        )
    if "classification of prospectuses" in text:
        return (
            "Updates the data used to classify prospectuses and the information "
            "that may be incorporated into them by reference."
        )
    if "restrictive measures" in text and "isil" in text:
        return (
            "Updates the EU financial sanctions measures applying to listed "
            "persons and entities associated with ISIL and Al-Qaida."
        )
    if "volume cap" in text and "transparency" in text:
        return (
            "Updates technical rules for the volume cap and the information "
            "used in transparency and related market calculations."
        )
    action_match = re.search(
        r"\b(?:supplementing|amending|laying down|concerning|on)\b\s+(.*)$",
        title,
        re.IGNORECASE,
    )
    subject = action_match.group(1).rstrip(".") if action_match else title
    return _shorten_text(
        f"Sets or changes EU financial-services requirements concerning "
        f"{subject}.",
        330,
    )


def _eurlex_description(
    title: str, resource_type: str, issued_date: str
) -> str:
    published = datetime.strptime(issued_date, "%d/%m/%Y").strftime(
        "%-d %B %Y"
        if os.name != "nt"
        else "%#d %B %Y"
    )
    return (
        f"Published in the EU Official Journal on {published}. "
        f"{_eurlex_plain_summary(title)}"
    )


def parse_eurlex_feed(data: Any) -> List[UpdateSummary]:
    try:
        bindings = data["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise MonitorError(
            "EUR-Lex returned an invalid Official Journal data response."
        ) from exc
    if not isinstance(bindings, list):
        raise MonitorError(
            "EUR-Lex returned an invalid Official Journal results list."
        )

    allowed_types = {
        "REG",
        "REG_DEL",
        "REG_IMPL",
        "DIR",
        "DIR_DEL",
        "DIR_IMPL",
        "DEC",
        "DEC_DEL",
        "DEC_IMPL",
        "RECO",
    }
    items: List[UpdateSummary] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        try:
            celex = str(binding["celex"]["value"]).strip()
            published_date = str(binding["date"]["value"]).strip()
            resource_type_url = str(binding["rtype"]["value"]).strip()
            title = re.sub(
                r"\s+", " ", str(binding["title"]["value"])
            ).strip()
        except (KeyError, TypeError):
            continue
        resource_type = resource_type_url.rstrip("/").rsplit("/", 1)[-1]
        issued_date = _normalise_date(published_date)
        if (
            not celex
            or not title
            or not issued_date
            or resource_type not in allowed_types
        ):
            continue
        url = (
            "https://eur-lex.europa.eu/legal-content/EN/TXT/"
            f"?uri=CELEX:{celex}"
        )
        description = _eurlex_description(
            title, resource_type, issued_date
        )
        source = (
            "EUR-Lex Official Journal — "
            f"{_eurlex_type_name(resource_type)}"
        )
        items.append(
            UpdateSummary(
                title=title,
                url=url,
                authority=EURLEX_AUTHORITY,
                source=source,
                update_type=_classify_update(title, description, source),
                issued_date=issued_date,
                description=description,
            )
        )
    items = _unique_items(items)
    items.sort(
        key=lambda item: (-_date_value(item.issued_date).toordinal(), item.title)
    )
    return items[:MAX_EURLEX_ITEMS]


def _unique_items(items: Iterable[UpdateSummary]) -> List[UpdateSummary]:
    unique: Dict[str, UpdateSummary] = {}
    for item in items:
        existing = unique.get(item.identifier)
        if existing is None:
            unique[item.identifier] = item
        elif item.description and not existing.description:
            unique[item.identifier] = item
        elif (
            item.update_type == "Consultation paper"
            and existing.update_type != "Consultation paper"
        ):
            unique[item.identifier] = item
    return list(unique.values())


def _description_is_useful(title: str, description: str) -> bool:
    if len(description.strip()) < 50:
        return False
    if description.rstrip().endswith(("...", "…")):
        return False
    normalised_title = _normalise_identity_text(title)
    normalised_description = _normalise_identity_text(description)
    return not (
        normalised_title == normalised_description
        or normalised_title.startswith(normalised_description)
        or (
            normalised_description.startswith(normalised_title)
            and len(normalised_description) <= len(normalised_title) + 20
        )
    )


def _fallback_description(item: UpdateSummary) -> str:
    topic = _topic_after_prefix(item.title)
    if item.update_type == "Consultation paper":
        return (
            f"{item.authority.split(' (', 1)[0]} is seeking stakeholder "
            f"feedback on {topic}."
        )
    return (
        f"{item.authority.split(' (', 1)[0]} published a regulatory update "
        f"concerning {topic}."
    )


def _detail_description(
    item: UpdateSummary,
    page_html: str,
) -> str:
    try:
        parser = group4_monitor.MetaDescriptionParser(item.title)
        parser.feed(page_html)
        parser.close()
        parsed_description = parser.result()
    except group4_monitor.MonitorError as exc:
        raise MonitorError(str(exc)) from exc

    description = item.description
    if item.authority == FSB_AUTHORITY:
        title_key = item.title.casefold()
        if "overview of consultation responses" in title_key:
            description = (
                "Summarises stakeholder feedback on the proposed criteria "
                "for deciding which insurers need recovery and resolution "
                "plans, including circumstances that should trigger those "
                "requirements."
            )
        elif "scope of insurers" in title_key and "final report" in title_key:
            description = (
                "Provides final guidance for authorities on deciding which "
                "insurers should maintain recovery and resolution plans and "
                "when those requirements should apply."
            )
        else:
            substantive_paragraphs = [
                paragraph
                for paragraph in parser.paragraphs
                if len(paragraph) >= 80
                and not paragraph.casefold().startswith(
                    ("responses should", "please submit", "questions for")
                )
            ]
            if substantive_paragraphs:
                description = substantive_paragraphs[0]
    if not _description_is_useful(item.title, description):
        description = parsed_description
    if not _description_is_useful(item.title, description):
        if (
            item.authority == FISMA_AUTHORITY
            and _classify_update(
                item.title, item.description, item.source
            ) == "Consultation paper"
        ):
            description = _external_fisma_consultation_description(item)
        else:
            description = _fallback_description(item)

    if (
        _classify_update(
            item.title, item.description, item.source
        )
        == "Consultation paper"
    ):
        deadline = _deadline_from_html(page_html)
        status_sentence = _consultation_status_sentence(deadline)
        if not status_sentence:
            status_sentence = (
                "The official page did not expose a response deadline "
                "to the monitor."
            )
        if status_sentence and status_sentence not in description:
            description = f"{description.rstrip()} {status_sentence}"
    return _shorten_text(description)


def _external_fisma_consultation_description(item: UpdateSummary) -> str:
    try:
        return group4_monitor._external_consultation_description(item.title)
    except group4_monitor.MonitorError as exc:
        raise MonitorError(str(exc)) from exc


def _fetch_missing_descriptions(
    items: List[UpdateSummary],
    timeout: float,
    fetcher: Callable[[str, float], str],
    fatf_fetcher: Callable[[str, float], str],
) -> Dict[str, str]:
    descriptions: Dict[str, str] = {}
    detail_items: List[UpdateSummary] = []
    for item in items:
        classified_type = _classify_update(
            item.title, item.description, item.source
        )
        needs_consultation_status = (
            classified_type == "Consultation paper"
            and item.authority
            in {FATF_AUTHORITY, FSB_AUTHORITY, FISMA_AUTHORITY}
        )
        if needs_consultation_status or (
            not _description_is_useful(item.title, item.description)
            and item.authority
            in {
                AMLA_AUTHORITY,
                FATF_AUTHORITY,
                FSB_AUTHORITY,
                FISMA_AUTHORITY,
            }
        ):
            detail_items.append(item)
        elif not _description_is_useful(item.title, item.description):
            descriptions[item.identifier] = _fallback_description(item)

    if not detail_items:
        return descriptions
    worker_count = min(MAX_DETAIL_WORKERS, len(detail_items))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                fatf_fetcher
                if item.authority == FATF_AUTHORITY
                else fetcher,
                item.url,
                timeout,
            ): item
            for item in detail_items
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                detail_html = future.result()
            except MonitorError:
                raise
            except Exception as exc:
                raise MonitorError(
                    f"Could not retrieve update detail {item.url}: {exc}"
                ) from exc
            descriptions[item.identifier] = _detail_description(
                item, detail_html
            )
    return descriptions


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
            f"Could not read saved update history {path}: {exc}"
        ) from exc
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(state.get("updates"), list)
    ):
        raise MonitorError(f"Saved update history is invalid: {path}")
    for record in state["updates"]:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("identifier"), str)
        ):
            raise MonitorError(
                f"Saved update history contains an invalid item: {path}"
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
            f"Could not save update history to {path}: {exc}"
        ) from exc


def _parse_fatf_items(
    publications_data: Any,
    consultations_data: Any,
) -> List[UpdateSummary]:
    publications = _shared_call(
        group5_monitor.parse_fatf_feed,
        publications_data,
        "FATF publications",
        MAX_FATF_PUBLICATIONS,
    )
    consultations = _shared_call(
        group5_monitor.parse_fatf_feed,
        consultations_data,
        "FATF public consultations",
        MAX_FATF_CONSULTATIONS,
        True,
    )
    return [
        _convert_shared_item(item)
        for item in publications + consultations
    ]


def _item_is_useful_and_recent(item: UpdateSummary) -> bool:
    if not _is_recent(item):
        return False
    if (
        item.authority == FISMA_AUTHORITY
        and item.title.casefold() in {"consolidated version", "media"}
    ):
        return False
    return True


def check_for_updates(
    state_path: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    text_fetcher: Optional[Callable[[str, float], str]] = None,
    json_fetcher: Optional[Callable[[str, float], Any]] = None,
    fatf_fetcher: Optional[Callable[[str, float], Any]] = None,
    fatf_text_fetcher: Optional[Callable[[str, float], str]] = None,
) -> List[RegulatoryUpdate]:
    if timeout <= 0:
        raise MonitorError("Timeout must be greater than zero.")
    get_text = text_fetcher or fetch_text
    get_json = json_fetcher or fetch_json
    get_fatf_json = fatf_fetcher or group5_monitor.fetch_fatf_json
    get_fatf_text = fatf_text_fetcher or fetch_fatf_text
    state = load_state(state_path)

    publications_url = ESRB_PUBLICATIONS_TEMPLATE.format(
        year=datetime.now(timezone.utc).year
    )
    eurlex_url = _build_eurlex_query_url(_eurlex_start_date(state))
    fatf_consultation_url = group5_monitor._build_fatf_consultation_url()

    # Fetch every official listing before changing history. A partial website
    # failure therefore cannot incorrectly mark unseen updates as read.
    esrb_publications_html = get_text(publications_url, timeout)
    esrb_recommendations_html = get_text(
        ESRB_RECOMMENDATIONS_URL, timeout
    )
    esrb_warnings_html = get_text(ESRB_WARNINGS_URL, timeout)
    amla_resources_html = get_text(AMLA_RESOURCES_URL, timeout)
    amla_consultations_html = get_text(AMLA_CONSULTATIONS_URL, timeout)
    try:
        fatf_publications_data = get_fatf_json(
            FATF_PUBLICATIONS_URL, timeout
        )
        fatf_consultations_data = get_fatf_json(
            fatf_consultation_url, timeout
        )
    except group5_monitor.MonitorError as exc:
        message = str(exc).replace("Group 5", "Group 6")
        message = message.replace(
            "requirements-group5.txt", "requirements-group6.txt"
        )
        raise MonitorError(message) from exc
    fsb_html = get_text(FSB_HOME_URL, timeout)
    esma_html = get_text(ESMA_LIBRARY_URL, timeout)
    fisma_consultations_html = get_text(
        FISMA_CONSULTATIONS_URL, timeout
    )
    fisma_publications_html = get_text(FISMA_PUBLICATIONS_URL, timeout)
    fisma_home_html = get_text(FISMA_HOME_URL, timeout)
    eurlex_data = get_json(eurlex_url, timeout)

    current_items: List[UpdateSummary] = []
    current_items.extend(
        parse_esrb_sources(
            esrb_publications_html,
            esrb_recommendations_html,
            esrb_warnings_html,
            publications_url,
        )
    )
    current_items.extend(
        parse_amla_sources(amla_resources_html, amla_consultations_html)
    )
    current_items.extend(
        _parse_fatf_items(
            fatf_publications_data, fatf_consultations_data
        )
    )
    current_items.extend(
        _convert_shared_item(item)
        for item in _shared_call(
            group4_monitor.parse_fsb_homepage, fsb_html
        )
    )
    current_items.extend(parse_esma_library(esma_html))
    current_items.extend(
        _convert_shared_item(item)
        for item in _shared_call(
            group4_monitor.parse_fisma_consultations,
            fisma_consultations_html,
        )
    )
    current_items.extend(
        _convert_shared_item(item)
        for item in _shared_call(
            group4_monitor.parse_fisma_publications,
            fisma_publications_html,
        )
    )
    current_items.extend(
        _convert_shared_item(item)
        for item in _shared_call(
            group4_monitor.parse_fisma_homepage, fisma_home_html
        )
    )
    current_items.extend(parse_eurlex_feed(eurlex_data))
    current_items = _unique_items(current_items)
    current_items = [
        item
        for item in current_items
        if _item_is_useful_and_recent(item)
    ]

    seen_identifiers = {
        record["identifier"]
        for record in state["updates"]
        if isinstance(record, dict)
        and isinstance(record.get("identifier"), str)
    }
    unseen_items = [
        item
        for item in current_items
        if item.identifier not in seen_identifiers
    ]
    detail_descriptions = _fetch_missing_descriptions(
        unseen_items, timeout, get_text, get_fatf_text
    )

    new_updates: List[RegulatoryUpdate] = []
    for item in unseen_items:
        description = detail_descriptions.get(
            item.identifier, item.description
        )
        if not item.issued_date or not description:
            raise MonitorError(
                f"An update was missing its date or description: {item.url}"
            )
        update_type = _classify_update(
            item.title, description, item.source
        )
        new_updates.append(
            RegulatoryUpdate(
                identifier=item.identifier,
                title=item.title,
                description=_shorten_text(description),
                issued_date=item.issued_date,
                url=item.url,
                authority=item.authority,
                source=item.source,
                update_type=update_type,
            )
        )

    authority_position = {
        authority: position
        for position, authority in enumerate(AUTHORITY_ORDER)
    }
    new_updates.sort(
        key=lambda update: (
            authority_position[update.authority],
            -_date_value(update.issued_date).toordinal(),
            update.title.casefold(),
        )
    )

    state["version"] = 1
    state["last_checked_utc"] = datetime.now(timezone.utc).isoformat()
    state["updates"].extend(asdict(update) for update in new_updates)
    save_state(state_path, state)
    return new_updates


def print_updates(updates: List[RegulatoryUpdate]) -> None:
    for authority_index, authority in enumerate(AUTHORITY_ORDER):
        authority_updates = [
            update for update in updates if update.authority == authority
        ]
        print(authority)
        print("=" * len(authority))
        if not authority_updates:
            print("No new updates available")
        else:
            print(f"{len(authority_updates)} new update(s)\n")
            for number, update in enumerate(authority_updates, start=1):
                print(f"{number}. {update.title}")
                print(f"   Type: {update.update_type}")
                print(f"   Source: {update.source}")
                print(f"   Date issued: {update.issued_date}")
                print(f"   Description: {update.description}")
                print(f"   Link: {update.url}")
                if number != len(authority_updates):
                    print()
        if authority_index != len(AUTHORITY_ORDER) - 1:
            print()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Print unseen Group 6 regulatory updates, grouped as ESRB, "
            "AMLA, FATF, FSB, ESMA, DG FISMA, then EUR-Lex."
        )
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=(
            "where to save update history "
            f"(default: {DEFAULT_STATE_FILE.name}, next to this script)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
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
        updates = check_for_updates(
            state_path=arguments.state_file,
            timeout=arguments.timeout,
        )
        print_updates(updates)
        return 0
    except MonitorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
