#!/usr/bin/env python3
"""Print unseen regulatory updates from the official Group 1 sources."""

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

# Group 1 shares the already-tested European Commission and EUR-Lex adapters
# with Groups 4 and 6. These files are deliberately kept in the same folder.
import group4 as group4_monitor
import group6 as group6_monitor


EBA_BASE_URL = "https://www.eba.europa.eu"
EBA_PUBLICATIONS_URL = (
    "https://www.eba.europa.eu/publications-and-media/publications"
)
EBA_CONSULTATIONS_URL = (
    "https://www.eba.europa.eu/publications-and-media/consultations"
)
EBA_DOCUMENT_TYPES: Tuple[Tuple[str, str], ...] = (
    ("245", "Decisions"),
    ("246", "Discussion papers"),
    ("247", "Draft Implementing Technical Standards"),
    ("248", "Draft Regulatory Technical Standards"),
    ("250", "Guidelines"),
    ("251", "Methodology"),
    ("252", "Opinions"),
    ("255", "Recommendations"),
    ("261", "Warnings"),
)
EBA_SPECIAL_SEARCHES: Tuple[Tuple[str, str], ...] = (
    ("validation", "Validation and reporting resources"),
    ("taxonomy", "Taxonomy and reporting resources"),
    ("reporting framework", "Reporting framework resources"),
)

ECB_HOME_URL = (
    "https://www.bankingsupervision.europa.eu/home/html/index.en.html"
)
ECB_PUBLICATIONS_RSS_URL = (
    "https://www.bankingsupervision.europa.eu/rss/pub.html"
)
ECB_PRESS_RSS_URL = (
    "https://www.bankingsupervision.europa.eu/rss/press.html"
)

FISMA_CONSULTATIONS_URL = group4_monitor.FISMA_CONSULTATIONS_URL
FISMA_PUBLICATIONS_URL = group4_monitor.FISMA_PUBLICATIONS_URL
FISMA_HOME_URL = group4_monitor.FISMA_HOME_URL
EURLEX_SPARQL_URL = group6_monitor.EURLEX_SPARQL_URL

EBA_AUTHORITY = "European Banking Authority (EBA)"
ECB_AUTHORITY = (
    "ECB Banking Supervision / Single Supervisory Mechanism (SSM)"
)
FISMA_AUTHORITY = group4_monitor.FISMA_AUTHORITY
EURLEX_AUTHORITY = group6_monitor.EURLEX_AUTHORITY
AUTHORITY_ORDER = (
    EBA_AUTHORITY,
    ECB_AUTHORITY,
    FISMA_AUTHORITY,
    EURLEX_AUTHORITY,
)

DEFAULT_STATE_FILE = Path(__file__).with_name("group1_updates_state.json")
DEFAULT_TIMEOUT_SECONDS = 45.0
RECENT_LOOKBACK_DAYS = 180
MAX_RESPONSE_BYTES = 12_000_000
MAX_DETAIL_WORKERS = 8
MAX_EBA_ITEMS = 50
MAX_ECB_ITEMS = 25
MAX_FISMA_ITEMS = 30
MAX_EBA_ITEMS_PER_LIST = 15
USER_AGENT = (
    "Mozilla/5.0 (compatible; Group1-Regulatory-Updates-Monitor/1.0; "
    "personal checker for official regulatory websites)"
)

REGULATORY_TERMS = re.compile(
    r"\b(?:"
    r"regulat(?:ion|ory)|directive|rules?|requirements?|"
    r"technical standards?|implementing standards?|"
    r"consultation|discussion paper|call for evidence|"
    r"supervis(?:ion|ory)|expectations?|guidance|guidelines?|"
    r"validation|data quality|data point model|DPM|XBRL|taxonomy|"
    r"reporting|disclosure|templates?|framework|"
    r"prudential|capital|liquidity|ICAAP|ILAAP|internal models?|"
    r"macroprudential|resolution|deposit guarantee|MiCA|CRR|CRD|"
    r"risk management|stress testing|governance|cybersecurity|"
    r"recommendation|opinion|warning|policy"
    r")\b",
    re.IGNORECASE,
)

EBA_RESOURCE_TERMS = re.compile(
    r"\b(?:"
    r"validation rules?|data point model|DPM(?:\s*2(?:\.0)?)?|"
    r"XBRL|taxonomy package|micro[_ -]?taxonomy|full[_ -]?taxonomy|"
    r"reporting framework|reporting package"
    r")\b",
    re.IGNORECASE,
)

ECB_PUBLICATION_TERMS = re.compile(
    r"\b(?:"
    r"guide|guidance|clarification|supervisory expectations?|"
    r"good practices?|framework|methodology|policy|policies|"
    r"macroprudential|letter to banks|reporting|cybersecurity|"
    r"internal models?|ICAAP|ILAAP|risk management|stress testing|"
    r"consultation"
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
    deadline: str = ""

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


def _clean_text(value: str) -> str:
    decoded = html_module.unescape(html_module.unescape(value))
    without_tags = re.sub(r"<[^>]+>", " ", decoded)
    without_zero_width = re.sub(r"[\u200b-\u200d\ufeff]", "", without_tags)
    return re.sub(r"\s+", " ", without_zero_width).strip()


def _normalise_identity_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _normalise_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/{2,}", "/", parsed.path)
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.casefold(),
            netloc=parsed.netloc.casefold(),
            path=path,
            fragment="",
        )
    )


def _official_url(base_url: str, href: str, hosts: Set[str]) -> str:
    absolute = _normalise_url(urljoin(base_url, href))
    parsed = urlparse(absolute)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in hosts:
        return ""
    return absolute


def _shorten_text(text: str, max_characters: int = 460) -> str:
    cleaned = re.sub(
        r"\s+", " ", re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    ).strip()
    if len(cleaned) <= max_characters:
        return cleaned
    shortened = cleaned[: max_characters + 1]
    sentence_end = max(
        shortened.rfind(marker) for marker in (". ", "? ", "! ")
    )
    if sentence_end >= 100:
        return shortened[: sentence_end + 1].strip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,;:") + "..."


def _normalise_date(value: str) -> str:
    text = _clean_text(value)
    if not text:
        return ""

    iso_match = re.search(
        r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", text
    )
    if iso_match:
        try:
            return datetime.strptime(
                iso_match.group(0), "%Y-%m-%d"
            ).strftime("%d/%m/%Y")
        except ValueError:
            pass

    slash_match = re.search(
        r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)", text
    )
    if slash_match:
        try:
            return datetime.strptime(
                slash_match.group(0), "%d/%m/%Y"
            ).strftime("%d/%m/%Y")
        except ValueError:
            pass

    for pattern in (
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        candidate = re.search(
            r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b"
            if pattern.startswith("%d")
            else r"\b[A-Za-z]+\s+\d{1,2},\s+\d{4}\b",
            text,
        )
        if not candidate:
            continue
        try:
            return datetime.strptime(
                candidate.group(0), pattern
            ).strftime("%d/%m/%Y")
        except ValueError:
            pass
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


def _is_recent(item: UpdateSummary) -> bool:
    parsed = _date_value(item.issued_date)
    return parsed != datetime.min and parsed.date() >= _recent_cutoff()


def _human_date(date_text: str) -> str:
    parsed = _date_value(date_text)
    if parsed == datetime.min:
        return date_text
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def _consultation_status(deadline: str) -> str:
    parsed = _date_value(deadline)
    if parsed == datetime.min:
        return (
            "The official listing did not expose a response deadline "
            "to the monitor."
        )
    if parsed.date() < datetime.now(timezone.utc).date():
        return f"The consultation closed on {_human_date(deadline)}."
    return f"The consultation is open until {_human_date(deadline)}."


def _classify_update(
    title: str, description: str, source: str, preferred: str = ""
) -> str:
    if preferred in {
        "Consultation paper",
        "Validation rules",
        "Reporting framework",
        "Regulation / standard-setting update",
        "Supervisory expectations / guidance",
        "Regulatory or policy update",
    }:
        return preferred

    title_key = title.casefold()
    source_key = source.casefold()
    text = " ".join((title, description, source)).casefold()
    if re.search(
        r"\b(?:response|responses|feedback)\s+(?:to|on)\b.*"
        r"\bconsultation\b|overview of consultation responses",
        title_key,
    ):
        return "Regulatory or policy update"
    if (
        title_key.startswith(
            (
                "consultation",
                "public consultation",
                "targeted consultation",
                "discussion paper",
            )
        )
        or "consultation paper" in title_key
        or "discussion paper" in source_key
        or (
            "consultation" in source_key
            and "response" not in title_key
        )
    ):
        return "Consultation paper"
    if "validation" in text or "data quality check" in text:
        return "Validation rules"
    if (
        "reporting framework" in text
        or "reporting package" in text
        or "reporting template" in text
        or "xbrl" in text
        or "taxonomy package" in text
        or "data point model" in text
    ):
        return "Reporting framework"
    if (
        "regulatory technical standard" in text
        or "implementing technical standard" in text
        or re.search(r"\b(?:rts|its)\b", text)
        or "regulation" in text
        or "directive" in text
        or "official journal" in source_key
    ):
        return "Regulation / standard-setting update"
    if (
        "supervisory" in text
        or "expectation" in text
        or "clarification" in text
        or "guidance" in text
        or "guideline" in text
        or "guide" in text
        or "good practices" in text
        or "letter to banks" in source_key
    ):
        return "Supervisory expectations / guidance"
    return "Regulatory or policy update"


def _description_is_useful(title: str, description: str) -> bool:
    cleaned = description.strip()
    if len(cleaned) < 60 or cleaned.endswith(("...", "…")):
        return False
    title_key = _normalise_identity_text(title)
    description_key = _normalise_identity_text(description)
    if description_key.startswith(
        "european banking supervisors contribute to keeping the banking "
        "system safe and sound"
    ):
        return False
    return not (
        title_key == description_key
        or title_key.startswith(description_key)
        or (
            description_key.startswith(title_key)
            and len(description_key) <= len(title_key) + 25
        )
    )


def _topic_from_title(title: str) -> str:
    topic = re.sub(
        r"^(?:public\s+|targeted\s+)?"
        r"(?:consultation|discussion)\s+(?:paper\s+)?(?:on\s+)?"
        r"(?:draft\s+)?",
        "",
        title,
        flags=re.IGNORECASE,
    )
    topic = re.sub(
        r"^(?:final\s+report\s+on\s+|final\s+|draft\s+)",
        "",
        topic,
        flags=re.IGNORECASE,
    )
    return topic.strip(" .:-") or title


def fetch_text(url: str, timeout: float) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/rss+xml,"
                "application/xml,text/xml;q=0.9,*/*;q=0.1"
            ),
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
                }:
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


def _publication_type_url(document_type: str) -> str:
    return f"{EBA_PUBLICATIONS_URL}?{urlencode({'document_type': document_type})}"


def _publication_search_url(search_text: str) -> str:
    return f"{EBA_PUBLICATIONS_URL}?{urlencode({'text': search_text})}"


def _extract_eba_cards(
    page_html: str, source_label: str, resource_search: bool = False
) -> List[UpdateSummary]:
    blocks = re.findall(
        r"<article\b[^>]*class=[\"'][^\"']*\bteaser\b[^\"']*[\"']"
        r"[^>]*>(.*?)</article>",
        page_html,
        re.IGNORECASE | re.DOTALL,
    )
    if not blocks:
        raise MonitorError(
            "Could not find EBA publication cards. "
            "The publications page layout may have changed."
        )

    items: List[UpdateSummary] = []
    for block in blocks:
        date_match = re.search(
            r"link-icon--calendar[^>]*>(.*?)</div>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        title_match = re.search(
            r"teaser__title[^>]*>\s*<a\b[^>]*href=[\"']"
            r"(?P<href>[^\"']+)[\"'][^>]*>(?P<title>.*?)</a>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if not date_match or not title_match:
            continue

        issued_date = _normalise_date(date_match.group(1))
        title = _clean_text(title_match.group("title"))
        document_url = _official_url(
            EBA_BASE_URL,
            title_match.group("href"),
            {"eba.europa.eu", "www.eba.europa.eu"},
        )
        press_match = re.search(
            r"<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>"
            r"\s*View press release\b",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        press_url = ""
        if press_match:
            press_url = _official_url(
                EBA_BASE_URL,
                press_match.group("href"),
                {"eba.europa.eu", "www.eba.europa.eu"},
            )
        if not title or not issued_date or not document_url:
            continue
        if resource_search and not EBA_RESOURCE_TERMS.search(title):
            continue
        if source_label == "Decisions" and not _eba_decision_is_relevant(title):
            continue

        preferred_types = {
            "Decisions": "Regulation / standard-setting update",
            "Discussion papers": "Consultation paper",
            "Draft Implementing Technical Standards": (
                "Regulation / standard-setting update"
            ),
            "Draft Regulatory Technical Standards": (
                "Regulation / standard-setting update"
            ),
            "Guidelines": "Supervisory expectations / guidance",
            "Methodology": "Supervisory expectations / guidance",
            "Opinions": "Regulatory or policy update",
            "Recommendations": "Supervisory expectations / guidance",
            "Warnings": "Supervisory expectations / guidance",
        }
        preferred = preferred_types.get(source_label, "")
        if resource_search:
            preferred = (
                "Validation rules"
                if re.search(r"\bvalidation\b", title, re.IGNORECASE)
                else "Reporting framework"
            )
        elif source_label == "Discussion papers":
            preferred = "Consultation paper"

        source = f"EBA Publications — {source_label}"
        url = press_url or document_url
        update_type = _classify_update(title, "", source, preferred)
        items.append(
            UpdateSummary(
                title=title,
                url=url,
                authority=EBA_AUTHORITY,
                source=source,
                update_type=update_type,
                issued_date=issued_date,
            )
        )
    return _unique_items(items)[:MAX_EBA_ITEMS_PER_LIST]


def _eba_decision_is_relevant(title: str) -> bool:
    text = title.casefold()
    excluded = (
        "board of appeal",
        "reimbursements and allowances",
        "annual accounts",
        "procurement",
        "management board",
        "board of supervisors",
        "internal rules",
    )
    return not any(term in text for term in excluded) and bool(
        REGULATORY_TERMS.search(title)
    )


def parse_eba_consultations(page_html: str) -> List[UpdateSummary]:
    blocks = re.findall(
        r"<article\b[^>]*data-element=[\"']event-teaser[\"'][^>]*>"
        r"(.*?)</article>",
        page_html,
        re.IGNORECASE | re.DOTALL,
    )
    if not blocks:
        raise MonitorError(
            "Could not find EBA consultation cards. "
            "The consultations page layout may have changed."
        )

    items: List[UpdateSummary] = []
    for block in blocks:
        link_match = re.search(
            r"<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*"
            r"class=[\"'][^\"']*teaser-event-calendar__calendar",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        title_match = re.search(
            r"data-field=[\"']title[\"'][^>]*>.*?<a\b[^>]*>"
            r".*?<span>(?P<title>.*?)</span>.*?</a>"
            r"(?:\s*<small>(?P<reference>.*?)</small>)?",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if not link_match or not title_match:
            continue

        days = [
            _clean_text(value)
            for value in re.findall(
                r"teaser-event-calendar__calendar-day[^>]*>(.*?)</span>",
                block,
                re.IGNORECASE | re.DOTALL,
            )
        ]
        months = [
            _clean_text(value)
            for value in re.findall(
                r"teaser-event-calendar__calendar-month[^>]*>(.*?)</span>",
                block,
                re.IGNORECASE | re.DOTALL,
            )
        ]
        year_match = re.search(
            r"teaser-event-calendar__calendar-year[^>]*>(.*?)</div>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        year_text = _clean_text(year_match.group(1)) if year_match else ""
        year_match_value = re.search(r"\b(20\d{2})\b", year_text)
        if not days or not months or not year_match_value:
            continue
        year = year_match_value.group(1)
        issued_date = ""
        deadline = ""
        if len(days) >= 2 and len(months) >= 2:
            issued_date = _normalise_date(
                f"{days[0]} {months[0]} {year}"
            )
            deadline = _normalise_date(f"{days[1]} {months[1]} {year}")
        else:
            # A single date on EBA discussion cards is the closing date, not
            # the publication date. The latter is resolved from the event's
            # related official news entry before recency filtering.
            deadline = _normalise_date(f"{days[0]} {months[0]} {year}")

        title = _clean_text(title_match.group("title"))
        reference = _clean_text(title_match.group("reference") or "")
        if reference and reference.casefold() not in title.casefold():
            title = f"{title} {reference}"
        url = _official_url(
            EBA_BASE_URL,
            link_match.group("href"),
            {"eba.europa.eu", "www.eba.europa.eu"},
        )
        if title and url:
            items.append(
                UpdateSummary(
                    title=title,
                    url=url,
                    authority=EBA_AUTHORITY,
                    source="EBA Consultations",
                    update_type="Consultation paper",
                    issued_date=issued_date,
                    deadline=deadline,
                )
            )
    if not items:
        raise MonitorError(
            "EBA consultation cards did not contain complete titles and dates."
        )
    return _unique_items(items)


def parse_ecb_rss(feed_xml: str, source_kind: str) -> List[UpdateSummary]:
    try:
        root = ET.fromstring(feed_xml.lstrip("\ufeff"))
    except ET.ParseError as exc:
        raise MonitorError("ECB returned an invalid RSS feed.") from exc

    items: List[UpdateSummary] = []
    for element in root.findall(".//item"):
        title = _clean_text(element.findtext("title", default=""))
        url = _official_url(
            ECB_HOME_URL,
            element.findtext("link", default=""),
            {
                "bankingsupervision.europa.eu",
                "www.bankingsupervision.europa.eu",
            },
        )
        date_text = element.findtext("pubDate", default="")
        issued_date = ""
        try:
            issued_date = parsedate_to_datetime(date_text).strftime("%d/%m/%Y")
        except (TypeError, ValueError, OverflowError):
            issued_date = _normalise_date(date_text)
        if not title or not url or not issued_date:
            continue

        if source_kind == "Publications":
            if not _ecb_publication_is_relevant(title):
                continue
            source = "ECB official publications feed"
        else:
            path = urlparse(url).path.casefold()
            if "/press/pr/" not in path or not _ecb_press_is_relevant(title):
                continue
            source = "ECB official press-release feed"
        items.append(
            UpdateSummary(
                title=title,
                url=url,
                authority=ECB_AUTHORITY,
                source=source,
                update_type=_classify_update(title, "", source),
                issued_date=issued_date,
            )
        )
    if source_kind == "Publications" and not items:
        raise MonitorError(
            "The ECB publication feed contained no relevant supervisory items."
        )
    return _unique_items(items)


def _ecb_publication_is_relevant(title: str) -> bool:
    text = title.casefold()
    excluded = (
        "letter from ",
        "list of supervised entities",
        "supervisory banking statistics",
        "written overview",
        "annual report",
        "financial statements",
    )
    return not any(term in text for term in excluded) and bool(
        ECB_PUBLICATION_TERMS.search(title)
    )


def _ecb_press_is_relevant(title: str) -> bool:
    text = title.casefold()
    excluded = (
        "sanctions ",
        "concludes asset quality review",
        "publishes supervisory banking statistics",
        "appoints ",
        "withdraws banking licence",
    )
    return not any(term in text for term in excluded) and bool(
        re.search(
            r"\b(?:consult|guidance|guide|supervisory expectations?|"
            r"reporting framework|regulatory framework|macroprudential|"
            r"polic(?:y|ies)|rules?|requirements?)\b",
            title,
            re.IGNORECASE,
        )
    )


def parse_ecb_homepage(page_html: str) -> List[UpdateSummary]:
    matches = re.findall(
        r"<dt\b[^>]*isoDate=[\"'](?P<date>[^\"']+)[\"'][^>]*>.*?</dt>"
        r"\s*<dd>(?P<body>.*?)</dd>",
        page_html,
        re.IGNORECASE | re.DOTALL,
    )
    if not matches:
        raise MonitorError(
            "Could not find ECB homepage publication entries. "
            "The homepage layout may have changed."
        )

    items: List[UpdateSummary] = []
    for raw_date, body in matches:
        category_match = re.search(
            r"<div\b[^>]*class=[\"'][^\"']*\bcategory\b[^\"']*[\"']"
            r"[^>]*>(.*?)</div>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        title_match = re.search(
            r"<div\b[^>]*class=[\"'][^\"']*\btitle\b[^\"']*[\"']"
            r"[^>]*>\s*<a\b[^>]*href=[\"'](?P<href>[^\"']+)"
            r"[\"'][^>]*>(?P<title>.*?)</a>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        if not category_match or not title_match:
            continue
        category = _clean_text(category_match.group(1))
        title = _clean_text(title_match.group("title"))
        if category.casefold() not in {
            "supervisory guides",
            "letter to banks",
            "governing council statement",
        }:
            continue
        if not _ecb_publication_is_relevant(title):
            continue
        url = _official_url(
            ECB_HOME_URL,
            title_match.group("href"),
            {
                "bankingsupervision.europa.eu",
                "www.bankingsupervision.europa.eu",
            },
        )
        issued_date = _normalise_date(raw_date)
        if not url or not issued_date:
            continue
        source = f"ECB homepage — {category.title()}"
        items.append(
            UpdateSummary(
                title=title,
                url=url,
                authority=ECB_AUTHORITY,
                source=source,
                update_type=_classify_update(title, "", source),
                issued_date=issued_date,
            )
        )
    return _unique_items(items)


def _convert_group4_item(item: Any) -> UpdateSummary:
    return UpdateSummary(
        title=item.title,
        url=item.url,
        authority=item.authority,
        source=f"DG FISMA — {item.source}",
        update_type=item.update_type,
        issued_date=item.issued_date,
        description=item.description,
    )


def _fisma_item_is_useful(item: UpdateSummary) -> bool:
    title = item.title.casefold()
    excluded = (
        "joint statement on the eu-u.s. joint financial regulatory forum",
        "joint statement on the eu-switzerland regulatory dialogues",
        "platform on sustainable finance response to the commission",
    )
    return not any(term in title for term in excluded)


def _convert_group6_item(item: Any) -> UpdateSummary:
    update_type = (
        "Reporting framework"
        if item.update_type == "Reporting framework"
        else "Regulation / standard-setting update"
    )
    return UpdateSummary(
        title=item.title,
        url=item.url,
        authority=item.authority,
        source=str(item.source).replace("â€”", "—"),
        update_type=update_type,
        issued_date=item.issued_date,
        description=item.description,
    )


def _unique_items(items: Iterable[UpdateSummary]) -> List[UpdateSummary]:
    unique: Dict[str, UpdateSummary] = {}
    for item in items:
        existing = unique.get(item.identifier)
        if existing is None:
            unique[item.identifier] = item
        elif item.deadline and not existing.deadline:
            unique[item.identifier] = item
        elif item.description and not existing.description:
            unique[item.identifier] = item

    # EBA sometimes lists both a final report and a consolidated document
    # against the same press release. Treat that as one regulatory change,
    # preferring the final/decision card. Discussion-event titles are also
    # matched to their equivalent "Discussion paper" publication title.
    eba_unique: Dict[Tuple[str, ...], UpdateSummary] = {}
    result: List[UpdateSummary] = []
    for item in unique.values():
        if item.authority != EBA_AUTHORITY:
            result.append(item)
            continue
        path = urlparse(item.url).path.casefold()
        if item.title.casefold().startswith(
            ("discussion on ", "discussion paper on ")
        ):
            subject = re.sub(
                r"^discussion\s+(?:paper\s+)?on\s+",
                "",
                item.title,
                flags=re.IGNORECASE,
            )
            subject = re.sub(r"\.(?:pdf|docx?)$", "", subject, flags=re.I)
            key = (
                "discussion",
                item.issued_date,
                _normalise_identity_text(subject),
            )
        elif "/publications-and-media/press-releases/" in path:
            key = ("press-release", item.issued_date, path.rstrip("/"))
        else:
            result.append(item)
            continue
        existing = eba_unique.get(key)
        if existing is None or _eba_preference_score(item) > _eba_preference_score(
            existing
        ):
            eba_unique[key] = item
    result.extend(eba_unique.values())
    return result


def _eba_preference_score(item: UpdateSummary) -> int:
    title = item.title.casefold()
    score = 2 if item.deadline else 0
    if title.startswith(("final report", "decision on ")):
        score += 2
    if title.startswith("consolidated"):
        score -= 2
    return score


def _extract_meta_description(title: str, page_html: str) -> str:
    try:
        parser = group4_monitor.MetaDescriptionParser(title)
        parser.feed(page_html)
        parser.close()
        return parser.result()
    except (group4_monitor.MonitorError, ValueError):
        return ""


def _related_eba_news_url(page_html: str) -> str:
    candidates = re.findall(
        r"<a\b[^>]*href=[\"'](?P<href>"
        r"/publications-and-media/(?:press-releases|news)/[^\"']+)"
        r"[\"'][^>]*>",
        page_html,
        re.IGNORECASE,
    )
    for href in candidates:
        url = _official_url(
            EBA_BASE_URL,
            href,
            {"eba.europa.eu", "www.eba.europa.eu"},
        )
        if url:
            return url
    return ""


def _deadline_from_eba_page(page_html: str) -> str:
    plain_text = _clean_text(page_html)
    match = re.search(
        r"Deadline for submitting responses:\s*"
        r"(\d{1,2}/\d{1,2}/\d{4})",
        plain_text,
        re.IGNORECASE,
    )
    return _normalise_date(match.group(1)) if match else ""


def _issued_date_from_eba_page(page_html: str) -> str:
    plain_text = _clean_text(page_html)
    match = re.search(
        r"\b(?:News|Press release)\s+"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b",
        plain_text,
        re.IGNORECASE,
    )
    return _normalise_date(match.group(1)) if match else ""


def _fallback_description(item: UpdateSummary) -> str:
    title_key = item.title.casefold()
    topic = _topic_from_title(item.title)
    if item.update_type == "Consultation paper":
        return (
            f"The EBA is seeking stakeholder feedback on {topic}."
            if item.authority == EBA_AUTHORITY
            else f"{item.authority} is seeking stakeholder feedback on {topic}."
        )
    if item.authority == EBA_AUTHORITY:
        if item.update_type == "Validation rules":
            version = re.search(r"\b\d+\.\d+\b", item.title)
            version_text = (
                f" version {version.group(0)}" if version else ""
            )
            return (
                "The EBA published validation rules for its reporting "
                f"framework{version_text}. Reporting institutions and "
                "software providers can use these consistency checks when "
                "preparing and validating regulatory data submissions."
            )
        if item.update_type == "Reporting framework":
            return (
                "The EBA published a machine-readable reporting resource "
                f"concerning {topic}. It supports implementation of the EBA "
                "reporting framework, including the definitions or taxonomy "
                "needed to prepare regulatory submissions."
            )
        source_kind = item.source.rsplit("—", 1)[-1].strip().lower()
        if "guideline" in source_kind:
            return (
                f"The EBA issued guidelines concerning {topic}. The document "
                "sets out the common approach that competent authorities and "
                "affected financial institutions should follow."
            )
        if "technical standard" in source_kind:
            return (
                f"The EBA published technical standards concerning {topic}. "
                "The document specifies how the relevant EU banking rules "
                "should be applied consistently in practice."
            )
        if "opinion" in source_kind:
            return (
                f"The EBA issued its formal opinion concerning {topic}, "
                "setting out the Authority's regulatory assessment and "
                "recommended approach."
            )
        return (
            f"The EBA published a regulatory or supervisory update "
            f"concerning {topic}."
        )
    if item.authority == ECB_AUTHORITY:
        if "icaaps and ilaaps" in title_key:
            return (
                "The ECB clarifies governance, content and submission "
                "expectations for banks' internal capital and liquidity "
                "adequacy assessment packages, helping banks keep ICAAP and "
                "ILAAP information coherent and well supported."
            )
        if "internal capital adequacy assessment process" in title_key:
            return (
                "The updated ICAAP Guide explains how supervised banks should "
                "assess and maintain adequate internal capital. It clarifies "
                "that management buffers reflect each bank's own capital "
                "planning needs and are not additional supervisory requirements."
            )
        if "materiality assessment" in title_key:
            return (
                "The guide explains how significant banks should assess "
                "whether changes or extensions to counterparty-credit-risk and "
                "credit-valuation-adjustment internal models are material."
            )
        if "assessment methodology" in title_key:
            return (
                "The guide explains how ECB supervisors assess banks' internal "
                "models for counterparty credit risk and credit valuation "
                "adjustment, including the depth and scope of model reviews."
            )
        if "guide to internal models" in title_key:
            return (
                "The ECB guide explains its supervisory expectations for the "
                "internal models banks use to calculate regulatory capital, "
                "supporting consistent model assessment across supervised banks."
            )
        if "climate and nature" in title_key and "stress testing" in title_key:
            return (
                "The ECB sets out observed good practices for incorporating "
                "climate- and nature-related risks into bank stress testing, "
                "including scenarios, governance and risk measurement."
            )
        if "climate and nature risk management" in title_key:
            return (
                "The ECB describes good practices banks can use to strengthen "
                "the identification, assessment and management of climate- and "
                "nature-related financial risks."
            )
        if "macroprudential policies" in title_key:
            return (
                "The ECB calls on authorities to preserve banking-system "
                "resilience amid elevated risks and supports simpler, more "
                "integrated macroprudential arrangements without weakening "
                "financial stability safeguards."
            )
        if "ai-enabled cybersecurity threats" in title_key:
            return (
                "The ECB alerts supervised banks to AI-enabled cyber threats "
                "and communicates supervisory expectations for identifying, "
                "managing and preparing for those operational-resilience risks."
            )
        if "streamlines supervisory guidance" in title_key:
            return (
                "The ECB has reviewed its supervisory publications, "
                "discontinuing outdated material and revising guidance so banks "
                "can more clearly identify the expectations that remain current."
            )
        if "banking sector capital framework" in title_key:
            return (
                "The ECB explains how the EU bank-capital framework combines "
                "risk-based capital requirements, the leverage ratio, "
                "supervisory requirements and capital buffers to ensure banks "
                "can absorb losses and remain resilient."
            )
        return (
            f"ECB Banking Supervision published supervisory guidance "
            f"concerning {topic}."
        )
    if item.authority == FISMA_AUTHORITY:
        shared = group4_monitor.UpdateSummary(
            title=item.title,
            url=item.url,
            authority=item.authority,
            source=item.source,
            update_type=item.update_type,
            issued_date=item.issued_date,
            description=item.description,
        )
        return group4_monitor._fallback_description(
            shared, item.update_type
        )
    return (
        f"{item.authority} published a regulatory update concerning {topic}."
    )


def _enrich_item(
    item: UpdateSummary,
    timeout: float,
    get_text: Callable[[str, float], str],
) -> UpdateSummary:
    description = item.description
    deadline = item.deadline

    if item.authority == EBA_AUTHORITY:
        if item.source == "EBA Consultations":
            try:
                event_html = get_text(item.url, timeout)
                deadline = _deadline_from_eba_page(event_html) or deadline
                description = _extract_meta_description(item.title, event_html)
                news_url = _related_eba_news_url(event_html)
                if news_url:
                    news_html = get_text(news_url, timeout)
                    news_description = _extract_meta_description(
                        item.title, news_html
                    )
                    if _description_is_useful(item.title, news_description):
                        description = news_description
            except MonitorError:
                pass
        elif not _description_is_useful(item.title, description):
            path = urlparse(item.url).path.casefold()
            if not re.search(
                r"\.(?:pdf|xlsx?|docx?|zip|csv)(?:$|\?)", path
            ):
                try:
                    page_html = get_text(item.url, timeout)
                    description = _extract_meta_description(
                        item.title, page_html
                    )
                    if item.update_type == "Consultation paper":
                        deadline = (
                            group4_monitor._deadline_from_html(page_html)
                            or deadline
                        )
                except MonitorError:
                    pass
    elif item.authority == ECB_AUTHORITY:
        path = urlparse(item.url).path.casefold()
        if path.endswith(".html"):
            try:
                page_html = get_text(item.url, timeout)
                parsed = _extract_meta_description(item.title, page_html)
                if _description_is_useful(item.title, parsed):
                    description = parsed
            except MonitorError:
                pass
    elif item.authority == FISMA_AUTHORITY:
        hostname = (urlparse(item.url).hostname or "").casefold()
        if (
            not _description_is_useful(item.title, description)
            and hostname == "finance.ec.europa.eu"
        ):
            try:
                page_html = get_text(item.url, timeout)
                shared = group4_monitor.UpdateSummary(
                    title=item.title,
                    url=item.url,
                    authority=item.authority,
                    source=item.source,
                    update_type=item.update_type,
                    issued_date=item.issued_date,
                    description=item.description,
                )
                description = group4_monitor._complete_description(
                    shared, page_html, item.update_type
                )
            except (MonitorError, group4_monitor.MonitorError):
                pass

    if not _description_is_useful(item.title, description):
        description = _fallback_description(item)
    if item.update_type == "Consultation paper" and not re.search(
        r"\b(?:open until|closed on|deadline)\b",
        description,
        re.IGNORECASE,
    ):
        status = _consultation_status(deadline)
        if status not in description:
            description = f"{description.rstrip()} {status}"
    return replace(
        item,
        description=_shorten_text(description),
        deadline=deadline,
        update_type=_classify_update(
            item.title, description, item.source, item.update_type
        ),
    )


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
            f"Could not read saved update history at {path}. "
            "Move or repair that file before running the monitor again."
        ) from exc
    if not isinstance(state, dict) or not isinstance(
        state.get("updates"), list
    ):
        raise MonitorError(f"Saved update history has an invalid format: {path}")
    for record in state["updates"]:
        if not isinstance(record, dict) or not isinstance(
            record.get("identifier"), str
        ):
            raise MonitorError(f"Saved update history is invalid: {path}")
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


def _cap_authority_items(items: List[UpdateSummary]) -> List[UpdateSummary]:
    limits = {
        EBA_AUTHORITY: MAX_EBA_ITEMS,
        ECB_AUTHORITY: MAX_ECB_ITEMS,
        FISMA_AUTHORITY: MAX_FISMA_ITEMS,
    }
    selected: List[UpdateSummary] = []
    for authority in AUTHORITY_ORDER:
        authority_items = [
            item for item in items if item.authority == authority
        ]
        authority_items.sort(
            key=lambda item: (
                -_date_value(item.issued_date).toordinal(),
                item.title.casefold(),
            )
        )
        selected.extend(
            authority_items[: limits.get(authority, len(authority_items))]
        )
    return selected


def check_for_updates(
    state_path: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    text_fetcher: Optional[Callable[[str, float], str]] = None,
    json_fetcher: Optional[Callable[[str, float], Any]] = None,
) -> List[RegulatoryUpdate]:
    if timeout <= 0:
        raise MonitorError("Timeout must be greater than zero.")
    get_text = text_fetcher or fetch_text
    get_json = json_fetcher or group6_monitor.fetch_json
    state = load_state(state_path)
    warnings: List[str] = []
    current_items: List[UpdateSummary] = []
    successful_authorities: Set[str] = set()

    try:
        eba_consultations_html = get_text(EBA_CONSULTATIONS_URL, timeout)
        eba_consultations = parse_eba_consultations(
            eba_consultations_html
        )
        resolved_consultations: List[UpdateSummary] = []
        for consultation in eba_consultations:
            if consultation.issued_date:
                resolved_consultations.append(consultation)
                continue
            try:
                event_html = get_text(consultation.url, timeout)
                issued_date = _issued_date_from_eba_page(event_html)
                deadline = (
                    _deadline_from_eba_page(event_html)
                    or consultation.deadline
                )
                if issued_date:
                    resolved_consultations.append(
                        replace(
                            consultation,
                            issued_date=issued_date,
                            deadline=deadline,
                        )
                    )
            except MonitorError as exc:
                warnings.append(
                    "EBA consultation date could not be resolved for "
                    f"{consultation.title}: {exc}"
                )
        current_items.extend(resolved_consultations)
        successful_authorities.add(EBA_AUTHORITY)
    except MonitorError as exc:
        warnings.append(f"EBA consultations could not be checked: {exc}")

    for document_type, label in EBA_DOCUMENT_TYPES:
        url = _publication_type_url(document_type)
        try:
            page_html = get_text(url, timeout)
            current_items.extend(_extract_eba_cards(page_html, label))
            successful_authorities.add(EBA_AUTHORITY)
        except MonitorError as exc:
            warnings.append(f"EBA {label.lower()} could not be checked: {exc}")

    for search_text, label in EBA_SPECIAL_SEARCHES:
        url = _publication_search_url(search_text)
        try:
            page_html = get_text(url, timeout)
            current_items.extend(
                _extract_eba_cards(
                    page_html, label, resource_search=True
                )
            )
            successful_authorities.add(EBA_AUTHORITY)
        except MonitorError as exc:
            warnings.append(f"EBA {label.lower()} could not be checked: {exc}")

    try:
        ecb_publications_xml = get_text(ECB_PUBLICATIONS_RSS_URL, timeout)
        current_items.extend(
            parse_ecb_rss(ecb_publications_xml, "Publications")
        )
        successful_authorities.add(ECB_AUTHORITY)
    except MonitorError as exc:
        warnings.append(f"ECB publications could not be checked: {exc}")
    try:
        ecb_press_xml = get_text(ECB_PRESS_RSS_URL, timeout)
        current_items.extend(parse_ecb_rss(ecb_press_xml, "Press"))
        successful_authorities.add(ECB_AUTHORITY)
    except MonitorError as exc:
        warnings.append(f"ECB press releases could not be checked: {exc}")
    try:
        ecb_home_html = get_text(ECB_HOME_URL, timeout)
        current_items.extend(parse_ecb_homepage(ecb_home_html))
        successful_authorities.add(ECB_AUTHORITY)
    except MonitorError as exc:
        warnings.append(f"ECB homepage could not be checked: {exc}")

    fisma_sources = (
        (
            "consultations",
            FISMA_CONSULTATIONS_URL,
            group4_monitor.parse_fisma_consultations,
        ),
        (
            "publications",
            FISMA_PUBLICATIONS_URL,
            group4_monitor.parse_fisma_publications,
        ),
        (
            "homepage",
            FISMA_HOME_URL,
            group4_monitor.parse_fisma_homepage,
        ),
    )
    for source_name, url, parser in fisma_sources:
        try:
            page_html = get_text(url, timeout)
            shared_items = parser(page_html)
            current_items.extend(
                _convert_group4_item(item) for item in shared_items
            )
            successful_authorities.add(FISMA_AUTHORITY)
        except (MonitorError, group4_monitor.MonitorError) as exc:
            warnings.append(f"DG FISMA {source_name} could not be checked: {exc}")

    eurlex_start = datetime.now(timezone.utc).date() - timedelta(
        days=RECENT_LOOKBACK_DAYS
    )
    eurlex_url = group6_monitor._build_eurlex_query_url(eurlex_start)
    try:
        eurlex_data = get_json(eurlex_url, timeout)
        shared_items = group6_monitor.parse_eurlex_feed(eurlex_data)
        current_items.extend(
            _convert_group6_item(item) for item in shared_items
        )
        successful_authorities.add(EURLEX_AUTHORITY)
    except (MonitorError, group6_monitor.MonitorError) as exc:
        warnings.append(f"EUR-Lex could not be checked: {exc}")

    if not successful_authorities:
        raise MonitorError(
            "No Group 1 source could be checked successfully. "
            + " ".join(warnings)
        )

    current_items = [
        item
        for item in _unique_items(current_items)
        if _is_recent(item)
        and (
            item.authority != FISMA_AUTHORITY
            or _fisma_item_is_useful(item)
        )
        and not (
            item.authority == FISMA_AUTHORITY
            and item.title.casefold() in {"consolidated version", "media"}
        )
    ]
    current_items = _cap_authority_items(current_items)

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

    enriched: Dict[str, UpdateSummary] = {}
    if unseen_items:
        with ThreadPoolExecutor(
            max_workers=min(MAX_DETAIL_WORKERS, len(unseen_items))
        ) as executor:
            futures = {
                executor.submit(
                    _enrich_item, item, timeout, get_text
                ): item
                for item in unseen_items
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    enriched[item.identifier] = future.result()
                except Exception as exc:
                    warnings.append(
                        f"Description enrichment failed for {item.title}: {exc}"
                    )
                    fallback = _fallback_description(item)
                    if item.update_type == "Consultation paper":
                        fallback = (
                            f"{fallback} "
                            f"{_consultation_status(item.deadline)}"
                        )
                    enriched[item.identifier] = replace(
                        item, description=_shorten_text(fallback)
                    )

    new_updates: List[RegulatoryUpdate] = []
    for item in unseen_items:
        completed = enriched.get(item.identifier, item)
        if not completed.issued_date or not completed.description:
            raise MonitorError(
                f"An update was missing its date or description: {item.url}"
            )
        new_updates.append(
            RegulatoryUpdate(
                identifier=completed.identifier,
                title=completed.title,
                description=completed.description,
                issued_date=completed.issued_date,
                url=completed.url,
                authority=completed.authority,
                source=completed.source,
                update_type=completed.update_type,
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

    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
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
            "Print unseen Group 1 regulatory updates, grouped as EBA, "
            "ECB Banking Supervision / SSM, European Commission DG FISMA, "
            "then EUR-Lex."
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
