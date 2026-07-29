#!/usr/bin/env python3
"""Print unseen regulatory updates from the official Group 5 sources."""

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
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


BCBS_PUBLICATIONS_URL = "https://www.bis.org/bcbs/publications.htm"
BIS_COMMITTEE_PUBLICATIONS_URL = "https://www.bis.org/publ/cmtpubl.htm"
FSB_HOME_URL = "https://www.fsb.org/"
FATF_HOME_URL = "https://www.fatf-gafi.org/en/home.html"
FATF_PUBLICATIONS_URL = "https://www.fatf-gafi.org/en/publications.html"
IFRS_HOME_URL = "https://www.ifrs.org/"
IFRS_NEWS_URL = "https://www.ifrs.org/news-and-events/news/"

BCBS_LIST_URL = "https://www.bis.org/api/document_lists/bcbspubls.json"
BIS_COMMITTEE_LISTS = (
    (
        "https://www.bis.org/api/document_lists/cpmi_publs.json",
        "Committee on Payments and Market Infrastructures (CPMI)",
        12,
    ),
    (
        "https://www.bis.org/api/document_lists/cgfs_publs.json",
        "Committee on the Global Financial System (CGFS)",
        5,
    ),
    (
        "https://www.bis.org/api/document_lists/mktc_publs.json",
        "Markets Committee",
        5,
    ),
)
FATF_RESULTS_URL = (
    "https://www.fatf-gafi.org/content/fatf-gafi/en/publications/"
    "jcr:content/root/container_1967587261/faceted_search/"
    "results.facets.json"
)
IFRS_NEWS_MODEL_URL = (
    "https://www.ifrs.org/content/ifrs/home/news-and-events/news/"
    "jcr:content/root/responsivegrid/mynewstile.model.json"
)

BCBS_AUTHORITY = "Basel Committee on Banking Supervision (BCBS)"
BIS_AUTHORITY = "Bank for International Settlements (BIS committees)"
FSB_AUTHORITY = "Financial Stability Board (FSB)"
FATF_AUTHORITY = "Financial Action Task Force (FATF)"
IFRS_AUTHORITY = "IFRS Foundation / IASB"
AUTHORITY_ORDER = (
    BCBS_AUTHORITY,
    BIS_AUTHORITY,
    FSB_AUTHORITY,
    FATF_AUTHORITY,
    IFRS_AUTHORITY,
)

DEFAULT_STATE_FILE = Path(__file__).with_name("group5_updates_state.json")
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 8_000_000
MAX_BCBS_ITEMS = 14
MAX_FATF_PUBLICATIONS = 8
MAX_FATF_CONSULTATIONS = 5
MAX_IFRS_ITEMS = 18
MAX_DETAIL_WORKERS = 8
USER_AGENT = (
    "Group5-Regulatory-Updates-Monitor/1.0 "
    "(personal checker for official regulatory websites)"
)

REGULATORY_TERMS = re.compile(
    r"\b(?:"
    r"regulat(?:ion|ory)|rule|requirements?|directive|standard|"
    r"consultation|consultative|exposure draft|"
    r"supervis(?:ion|ory)|expectation|guidance|guideline|principles?|"
    r"validation|pre-validation|data quality|assessment|methodology|"
    r"reporting|reporting framework|disclosure|taxonomy|template|"
    r"implementation|monitoring|amendment|frequently asked questions|"
    r"prudential|capital|liquidity|margin|market risk|operational risk|"
    r"financial stability|payment|settlement|cross-border|ISO 20022|"
    r"PFMI|Basel|AML|CFT|money laundering|terrorist financing|"
    r"risk management|sound practices|recommendation"
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


def _classes(attributes: Dict[str, str]) -> Set[str]:
    return set(attributes.get("class", "").split())


def _clean_text(parts: Iterable[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _strip_markup(text: str) -> str:
    parser = PlainTextParser()
    parser.feed(text)
    parser.close()
    return _clean_text(parser.parts)


def _normalise_identity_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


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
            query="",
            fragment="",
        )
    )


def _safe_url(
    base_url: str,
    href: str,
    allowed_hosts: Set[str],
    required_prefixes: Tuple[str, ...] = ("/",),
) -> Optional[str]:
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    hostname = (parsed.hostname or "").casefold()
    path = re.sub(r"/{2,}", "/", parsed.path)
    if parsed.scheme != "https" or hostname not in allowed_hosts:
        return None
    if not any(path.startswith(prefix) for prefix in required_prefixes):
        return None
    return _normalise_url(urlunparse(parsed._replace(path=path)))


def _shorten_text(text: str, max_characters: int = 420) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_characters:
        return cleaned
    shortened = cleaned[: max_characters + 1]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,;:") + "..."


def _normalise_date(date_text: str) -> str:
    value = re.sub(r"\s+", " ", date_text).strip()
    if not value:
        return ""

    iso_match = re.search(
        r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", value
    )
    if iso_match:
        try:
            return datetime.strptime(
                iso_match.group(0), "%Y-%m-%d"
            ).strftime("%d/%m/%Y")
        except ValueError:
            pass

    value = re.sub(
        r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    date_match = re.search(
        r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", value
    )
    if date_match:
        candidate = " ".join(date_match.groups())
        for date_format in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(candidate, date_format).strftime(
                    "%d/%m/%Y"
                )
            except ValueError:
                continue

    slash_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", value)
    if slash_match:
        try:
            return datetime.strptime(
                slash_match.group(0), "%d/%m/%Y"
            ).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return ""


def _fatf_epoch_date(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    try:
        # FATF stores local midnight as a UTC epoch. Moving to midday prevents
        # the displayed publication date becoming the previous UTC date.
        issued = datetime.fromtimestamp(
            value / 1000, tz=timezone.utc
        ) + timedelta(hours=12)
        return issued.strftime("%d/%m/%Y")
    except (OSError, OverflowError, ValueError):
        return ""


def _classify_update(title: str, description: str, source: str) -> str:
    text = " ".join((title, description, source)).casefold()
    if "consultation" in text or "consultative" in text or "exposure draft" in text:
        return "Consultation paper"
    if (
        "validation" in text
        or "pre-validation" in text
        or "data quality" in text
        or "implementation monitoring" in text
        or "regulatory consistency assessment" in text
        or re.search(r"\bassessment(?:s)?\b", text)
        or "assessment methodology" in text
        or "quantitative impact study" in text
        or re.search(r"\bqis\b", text)
    ):
        return "Validation rules / assessment"
    if (
        "reporting framework" in text
        or "reporting requirement" in text
        or "reporting template" in text
        or "risk data aggregation" in text
        or "disclosure" in text
        or "taxonomy" in text
        or "iso 20022" in text
    ):
        return "Reporting framework"
    if (
        "supervisory" in text
        or "expectation" in text
        or "guidance" in text
        or "guideline" in text
        or "sound practice" in text
        or "risk management" in text
        or "recommendation" in text
    ):
        return "Supervisory expectations / guidance"
    if (
        "standard" in text
        or "amendment" in text
        or "rule" in text
        or "principle" in text
    ):
        return "Regulation / standard-setting update"
    return "Regulatory or policy update"


class PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class FsbHomepageParser(HTMLParser):
    """Extract the official FSB Publications and Consultations cards."""

    SECTIONS = {
        "publications": "Policy publication",
        "consultations": "Consultation",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: List[UpdateSummary] = []
        self.found_sections: Set[str] = set()
        self._section = ""
        self._section_depth = 0
        self._item: Optional[Dict[str, str]] = None
        self._item_div_depth = 0
        self._capture = ""
        self._parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "section":
            section_id = attributes.get("id", "")
            if not self._section and section_id in self.SECTIONS:
                self._section = section_id
                self._section_depth = 1
                self.found_sections.add(section_id)
            elif self._section:
                self._section_depth += 1

        if not self._section:
            return

        if tag == "div":
            if self._item is not None:
                self._item_div_depth += 1
            elif "post-excerpt-compact" in classes:
                self._item = {"title": "", "url": "", "date": ""}
                self._item_div_depth = 1

        if self._item is None:
            return

        if tag == "a" and not self._item["url"]:
            url = _safe_url(
                FSB_HOME_URL,
                attributes.get("href", ""),
                {"fsb.org", "www.fsb.org"},
            )
            if url and re.match(r"^/\d{4}/\d{2}/", urlparse(url).path):
                self._item["url"] = url
                self._capture = "title"
                self._parts = []
        elif tag == "span" and not self._item["date"]:
            self._capture = "date"
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._section:
            return

        if self._item is not None:
            if tag == "a" and self._capture == "title":
                self._item["title"] = _clean_text(self._parts)
                self._capture = ""
            elif tag == "span" and self._capture == "date":
                self._item["date"] = _normalise_date(
                    _clean_text(self._parts)
                )
                self._capture = ""

            if tag == "div":
                self._item_div_depth -= 1
                if self._item_div_depth == 0:
                    item = self._item
                    if all(item.values()):
                        source = self.SECTIONS[self._section]
                        self.items.append(
                            UpdateSummary(
                                title=item["title"],
                                url=item["url"],
                                authority=FSB_AUTHORITY,
                                source=source,
                                update_type=_classify_update(
                                    item["title"], "", source
                                ),
                                issued_date=item["date"],
                            )
                        )
                    self._item = None
                    self._capture = ""

        if tag == "section":
            self._section_depth -= 1
            if self._section_depth == 0:
                self._section = ""


class OfficialDescriptionParser(HTMLParser):
    """Extract a concise, substantive description from an official detail page."""

    EXCLUDED_PHRASES = (
        "you need to sign in",
        "cookie preferences",
        "your privacy",
        "registered trade marks",
        "register for news alerts",
        "all rights reserved",
    )

    def __init__(self, title: str) -> None:
        super().__init__(convert_charrefs=True)
        self.title = _normalise_identity_text(title)
        self.meta_descriptions: List[str] = []
        self.embedded_descriptions: List[str] = []
        self.paragraphs: List[str] = []
        self._main_depth = 0
        self._text_div_depth = 0
        self._capture = False
        self._parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        name = attributes.get("name", "").casefold()
        prop = attributes.get("property", "").casefold()
        if tag == "meta" and (
            name == "description" or prop == "og:description"
        ):
            value = re.sub(
                r"\s+", " ", attributes.get("content", "")
            ).strip()
            if value:
                self.meta_descriptions.append(value)

        # Some BIS detail pages render their article with JavaScript, but the
        # official article text is supplied in a JSON data attribute.
        embedded_json = attributes.get("data-react-props", "")
        if embedded_json and '"document"' in embedded_json:
            try:
                payload = json.loads(embedded_json)
                document = payload.get("document", {})
                if isinstance(document, dict):
                    content = document.get("content") or document.get("abstract")
                    if isinstance(content, str) and content.strip():
                        value = _strip_markup(content)
                        if value:
                            self.embedded_descriptions.append(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        if tag in {"main", "article"}:
            self._main_depth += 1
        elif tag == "div":
            if self._text_div_depth:
                self._text_div_depth += 1
            elif "cmp-text" in _classes(attributes):
                self._text_div_depth = 1

        if (
            tag in {"p", "blockquote"}
            and (self._main_depth or self._text_div_depth)
            and not self._capture
        ):
            self._capture = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "blockquote"} and self._capture:
            value = _clean_text(self._parts)
            if value:
                self.paragraphs.append(value)
            self._capture = False

        if tag in {"main", "article"} and self._main_depth:
            self._main_depth -= 1
        elif tag == "div" and self._text_div_depth:
            self._text_div_depth -= 1

    def _is_useful(self, value: str) -> bool:
        normalised = _normalise_identity_text(value)
        return (
            len(value) >= 55
            and normalised != self.title
            and not any(phrase in normalised for phrase in self.EXCLUDED_PHRASES)
        )

    def result(self) -> str:
        useful_paragraphs = [
            value for value in self.paragraphs if self._is_useful(value)
        ]
        if useful_paragraphs:
            first = useful_paragraphs[0]
            if len(first) < 130 and len(useful_paragraphs) > 1:
                combined = f"{first} {useful_paragraphs[1]}"
                return _shorten_text(combined)
            return _shorten_text(first)

        for value in self.embedded_descriptions:
            if self._is_useful(value):
                return _shorten_text(value)

        for value in self.meta_descriptions:
            if self._is_useful(value):
                return _shorten_text(value)
        return ""


def fetch_text(url: str, timeout: float) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        },
    )
    return _fetch_urllib(request, url, timeout, {"text/html", "application/xhtml+xml"})


def fetch_json(url: str, timeout: float) -> Any:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,*/*;q=0.1",
        },
    )
    text = _fetch_urllib(
        request,
        url,
        timeout,
        {"application/json", "text/json", "text/plain"},
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Official feed returned invalid JSON: {url}") from exc


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


def fetch_fatf_json(url: str, timeout: float) -> Any:
    """Fetch FATF's public JSON feed through its browser-protection layer."""

    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise MonitorError(
            "FATF requires the curl_cffi package. Run this once in the "
            "Group 5 folder: python -m pip install -r requirements-group5.txt"
        ) from exc

    last_error = ""
    for impersonation in ("chrome124", "safari"):
        try:
            response = curl_requests.get(
                url,
                timeout=timeout,
                impersonate=impersonation,
                headers={
                    "Accept": "application/json",
                    "Referer": FATF_PUBLICATIONS_URL,
                },
            )
            if response.status_code == 200:
                encoded = response.content
                if len(encoded) > MAX_RESPONSE_BYTES:
                    raise MonitorError(
                        "The FATF response was unexpectedly large."
                    )
                try:
                    return response.json()
                except (ValueError, json.JSONDecodeError) as exc:
                    raise MonitorError(
                        "FATF returned invalid publication data."
                    ) from exc
            last_error = f"HTTP {response.status_code}"
        except MonitorError:
            raise
        except Exception as exc:  # curl_cffi has version-specific exceptions
            last_error = str(exc)
    raise MonitorError(f"Could not retrieve the FATF publication feed: {last_error}")


def _bis_detail_url(path: str) -> str:
    clean_path = re.sub(r"\.(?:htm|html|pdf)$", "", path, flags=re.IGNORECASE)
    return _normalise_url(urljoin("https://www.bis.org/", clean_path + ".htm"))


def _bis_records(data: Any, source_name: str) -> List[Dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("list"), dict):
        raise MonitorError(f"BIS returned an invalid {source_name} publication feed.")
    records = [
        record
        for record in data["list"].values()
        if isinstance(record, dict)
    ]
    records.sort(
        key=lambda record: str(record.get("publication_start_date", "")),
        reverse=True,
    )
    return records


def parse_bcbs_feed(data: Any) -> List[UpdateSummary]:
    allowed_types = {
        "standards",
        "guidelines",
        "sound practices",
        "consultative",
        "implementation reports",
        "faqs",
        "qis",
        "newsletters",
    }
    items: List[UpdateSummary] = []
    for record in _bis_records(data, "BCBS"):
        title = html_module.unescape(
            str(record.get("short_title") or record.get("long_title") or "")
        ).strip()
        path = str(record.get("path") or "")
        issued_date = _normalise_date(
            str(record.get("publication_start_date") or "")
        )
        publication_type = str(record.get("publication_type") or "Publication")
        type_key = publication_type.casefold()
        if not title or not path or not issued_date:
            continue
        if type_key == "working papers":
            continue
        if type_key not in allowed_types and not REGULATORY_TERMS.search(title):
            continue
        source = f"BCBS publications — {publication_type}"
        items.append(
            UpdateSummary(
                title=title,
                url=_bis_detail_url(path),
                authority=BCBS_AUTHORITY,
                source=source,
                update_type=_classify_update(title, "", source),
                issued_date=issued_date,
                description=_strip_markup(str(record.get("abstract") or "")),
            )
        )
        if len(items) >= MAX_BCBS_ITEMS:
            break
    if not items:
        raise MonitorError("No current BCBS regulatory publications were found.")
    return items


def _is_relevant_bis_committee_item(title: str, source_name: str) -> bool:
    if source_name == "Committee on Payments and Market Infrastructures (CPMI)":
        if "cash still" in title.casefold() and "commentary" in title.casefold():
            return False
        return bool(
            REGULATORY_TERMS.search(title)
            or re.search(
                r"\b(?:financial messaging|financial market infrastructure|"
                r"central counterparty|initial margin|cross-border payments?)\b",
                title,
                re.IGNORECASE,
            )
        )
    return bool(
        REGULATORY_TERMS.search(title)
        or re.search(
            r"\b(?:funding|financial markets?|central bank|foreign currency|"
            r"debt|capital flows?|housing|interest rate|market intelligence|"
            r"monetary policy|FX|foreign exchange)\b",
            title,
            re.IGNORECASE,
        )
    )


def parse_bis_committee_feed(
    data: Any, source_name: str, limit: int
) -> List[UpdateSummary]:
    items: List[UpdateSummary] = []
    for record in _bis_records(data, source_name):
        title = html_module.unescape(
            str(record.get("short_title") or record.get("long_title") or "")
        ).strip()
        path = str(record.get("path") or "")
        issued_date = _normalise_date(
            str(record.get("publication_start_date") or "")
        )
        if (
            not title
            or not path
            or not issued_date
            or not _is_relevant_bis_committee_item(title, source_name)
        ):
            continue
        source = f"BIS committee publications — {source_name}"
        items.append(
            UpdateSummary(
                title=title,
                url=_bis_detail_url(path),
                authority=BIS_AUTHORITY,
                source=source,
                update_type=_classify_update(title, "", source),
                issued_date=issued_date,
                description=_strip_markup(str(record.get("abstract") or "")),
            )
        )
        if len(items) >= limit:
            break
    if not items:
        raise MonitorError(
            f"No relevant publications were found for {source_name}."
        )
    return items


def parse_fsb_homepage(html: str) -> List[UpdateSummary]:
    parser = FsbHomepageParser()
    parser.feed(html)
    parser.close()
    if parser.found_sections != set(FsbHomepageParser.SECTIONS):
        raise MonitorError(
            "Could not find both FSB Publications and Consultations sections. "
            "The homepage layout may have changed."
        )
    items = _unique_items(parser.items)
    if not items:
        raise MonitorError("No complete FSB publication cards were found.")
    return items


def _fatf_item_is_relevant(title: str, description: str) -> bool:
    text = f"{title} {description}"
    excluded = re.search(
        r"\b(?:event recording|takes over presidency|event:|webinar|"
        r"speech|vacancy|job opening)\b",
        text,
        re.IGNORECASE,
    )
    return not excluded and bool(REGULATORY_TERMS.search(text))


def parse_fatf_feed(
    data: Any, source: str, limit: int, consultation: bool = False
) -> List[UpdateSummary]:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise MonitorError("FATF returned an invalid publication feed.")
    items: List[UpdateSummary] = []
    for record in data["results"]:
        if not isinstance(record, dict):
            continue
        title = _strip_markup(str(record.get("title") or ""))
        description = _strip_markup(str(record.get("description") or ""))
        path = str(record.get("path") or "")
        issued_date = _fatf_epoch_date(record.get("publicationDate"))
        url = _safe_url(
            FATF_PUBLICATIONS_URL,
            path + ("" if path.endswith(".html") else ".html"),
            {"fatf-gafi.org", "www.fatf-gafi.org"},
            ("/content/fatf-gafi/en/publications/", "/en/publications/"),
        )
        if not title or not description or not issued_date or not url:
            continue
        if not consultation and not _fatf_item_is_relevant(title, description):
            continue
        items.append(
            UpdateSummary(
                title=title,
                url=url,
                authority=FATF_AUTHORITY,
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
        if len(items) >= limit:
            break
    if not items:
        raise MonitorError(f"No complete FATF items were found in {source}.")
    return items


def _ifrs_item_is_relevant(item_type: str, title: str) -> bool:
    type_key = item_type.casefold()
    text = title.casefold()
    if type_key in {
        "amendment",
        "consultation",
        "ifrs taxonomy",
        "ifrs taxonomy update",
        "standard",
    }:
        return True
    if type_key == "update" and re.search(r"\b(?:IASB|ISSB|IFRIC)\b", title):
        return True
    if type_key == "announcement":
        if re.search(
            r"\b(?:webcast|module|training|conference|appointment|"
            r"educational|podcast|previews?)\b",
            title,
            re.IGNORECASE,
        ):
            return False
        return bool(
            re.search(r"\b(?:IASB|ISSB|IFRS)\b", title)
            and re.search(
                r"\b(?:decisions?|issues?|proposes?|publishes?|standard|taxonomy|"
                r"disclosure|reporting|amendment|guidance)\b",
                title,
                re.IGNORECASE,
            )
        )
    return bool(
        REGULATORY_TERMS.search(title)
        and re.search(r"\b(?:IASB|ISSB|IFRS|IFRIC)\b", title)
        and type_key not in {
            "appointment",
            "conference",
            "meeting",
            "podcast",
            "speech",
            "translated content",
            "video",
            "webcast",
        }
    )


def parse_ifrs_feed(data: Any) -> List[UpdateSummary]:
    if not isinstance(data, dict) or not isinstance(data.get("resultList"), list):
        raise MonitorError("IFRS returned an invalid news feed.")
    items: List[UpdateSummary] = []
    for record in data["resultList"]:
        if not isinstance(record, dict):
            continue
        item_type = _strip_markup(str(record.get("title") or "Update"))
        title = _strip_markup(str(record.get("description") or ""))
        issued_date = _normalise_date(str(record.get("date") or ""))
        path = str(record.get("uri") or "")
        if not _ifrs_item_is_relevant(item_type, title):
            continue
        url = _safe_url(
            IFRS_NEWS_URL,
            path + (
                ""
                if path.endswith((".html", ".pdf"))
                else ".html"
            ),
            {"ifrs.org", "www.ifrs.org"},
            ("/content/ifrs/", "/news-and-events/", "/projects/"),
        )
        if not title or not issued_date or not url:
            continue
        source = f"IFRS Foundation news — {item_type}"
        items.append(
            UpdateSummary(
                title=title,
                url=url,
                authority=IFRS_AUTHORITY,
                source=source,
                update_type=_classify_update(title, "", source),
                issued_date=issued_date,
            )
        )
        if len(items) >= MAX_IFRS_ITEMS:
            break
    if not items:
        raise MonitorError("No relevant IFRS Foundation / IASB updates were found.")
    return items


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


def _description_from_detail(item: UpdateSummary, html: str) -> str:
    parser = OfficialDescriptionParser(item.title)
    parser.feed(html)
    parser.close()
    description = parser.result()
    if not description:
        raise MonitorError(
            "Could not extract the important short description for "
            f"this update: {item.url}"
        )
    return description


def _empty_state() -> Dict[str, Any]:
    return {"version": 1, "last_checked_utc": None, "updates": []}


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        with path.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Could not read saved update history {path}: {exc}") from exc

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


def _date_sort_key(update: RegulatoryUpdate) -> datetime:
    try:
        return datetime.strptime(update.issued_date, "%d/%m/%Y")
    except ValueError:
        return datetime.min


def _build_ifrs_feed_url() -> str:
    query = urlencode(
        {
            # "All" is IFRS's own "Most recent" view. It also avoids an empty
            # feed at the beginning of a new calendar year.
            "filterYear": "All",
            "filterType": "All",
            "filterByFollowTag": "false",
            "style": "listStyle",
        }
    )
    return f"{IFRS_NEWS_MODEL_URL}?{query}"


def _build_fatf_consultation_url() -> str:
    query = urlencode(
        {
            "facet": "fatf-gafi-faft-doc types:tag-Public consultation",
            "offset": "0",
        }
    )
    return f"{FATF_RESULTS_URL}?{query}"


def _fetch_missing_descriptions(
    items: List[UpdateSummary],
    timeout: float,
    fetcher: Callable[[str, float], str],
) -> Dict[str, str]:
    descriptions: Dict[str, str] = {}
    detail_items = [item for item in items if not item.description]
    if not detail_items:
        return descriptions

    worker_count = min(MAX_DETAIL_WORKERS, len(detail_items))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(fetcher, item.url, timeout): item
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
            descriptions[item.identifier] = _description_from_detail(
                item, detail_html
            )
    return descriptions


def check_for_updates(
    state_path: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    text_fetcher: Optional[Callable[[str, float], str]] = None,
    json_fetcher: Optional[Callable[[str, float], Any]] = None,
    fatf_fetcher: Optional[Callable[[str, float], Any]] = None,
) -> List[RegulatoryUpdate]:
    if timeout <= 0:
        raise MonitorError("Timeout must be greater than zero.")
    get_text = text_fetcher or fetch_text
    get_json = json_fetcher or fetch_json
    get_fatf_json = fatf_fetcher or fetch_fatf_json

    # Every listing is fetched and parsed before history is changed. A partial
    # website failure therefore cannot incorrectly mark updates as seen.
    bcbs_data = get_json(BCBS_LIST_URL, timeout)
    bis_committee_data = [
        (source_name, limit, get_json(url, timeout))
        for url, source_name, limit in BIS_COMMITTEE_LISTS
    ]
    fsb_html = get_text(FSB_HOME_URL, timeout)
    fatf_publications_data = get_fatf_json(FATF_RESULTS_URL, timeout)
    fatf_consultations_data = get_fatf_json(
        _build_fatf_consultation_url(), timeout
    )
    ifrs_data = get_json(_build_ifrs_feed_url(), timeout)

    current_items: List[UpdateSummary] = []
    current_items.extend(parse_bcbs_feed(bcbs_data))
    for source_name, limit, data in bis_committee_data:
        current_items.extend(
            parse_bis_committee_feed(data, source_name, limit)
        )
    current_items.extend(parse_fsb_homepage(fsb_html))
    current_items.extend(
        parse_fatf_feed(
            fatf_publications_data,
            "FATF publications",
            MAX_FATF_PUBLICATIONS,
        )
    )
    current_items.extend(
        parse_fatf_feed(
            fatf_consultations_data,
            "FATF public consultations",
            MAX_FATF_CONSULTATIONS,
            consultation=True,
        )
    )
    current_items.extend(parse_ifrs_feed(ifrs_data))
    current_items = _unique_items(current_items)

    state = load_state(state_path)
    seen_identifiers = {
        record["identifier"]
        for record in state["updates"]
        if isinstance(record, dict)
        and isinstance(record.get("identifier"), str)
    }
    unseen_items = [
        item for item in current_items if item.identifier not in seen_identifiers
    ]

    detail_descriptions = _fetch_missing_descriptions(
        unseen_items, timeout, get_text
    )
    new_updates: List[RegulatoryUpdate] = []
    for item in unseen_items:
        description = item.description or detail_descriptions.get(
            item.identifier, ""
        )
        description = re.sub(
            r"^(?:Highlights|Note)\s+", "", description, flags=re.IGNORECASE
        )
        if not item.issued_date or not description:
            raise MonitorError(
                f"An update was missing its date or description: {item.url}"
            )
        update_type = _classify_update(
            item.title, description, item.source
        )
        if item.update_type in {
            "Consultation paper",
            "Validation rules / assessment",
            "Reporting framework",
        }:
            update_type = item.update_type
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
            -_date_sort_key(update).toordinal(),
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
            "Print unseen Group 5 regulatory updates, grouped as BCBS, "
            "BIS committees, FSB, FATF, then IFRS Foundation / IASB."
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
