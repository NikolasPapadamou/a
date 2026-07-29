#!/usr/bin/env python3
"""Print unseen regulatory updates from the official Group 4 sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


SRB_HOME_URL = "https://www.srb.europa.eu/en"
SRB_CONSULTATIONS_URL = (
    "https://www.srb.europa.eu/en/content/engagement-and-consultations"
)
SRB_REPORTING_URL = (
    "https://www.srb.europa.eu/en/content/2026-resolution-reporting"
)
FISMA_CONSULTATIONS_URL = (
    "https://finance.ec.europa.eu/regulation-and-supervision/consultation_en"
)
FISMA_PUBLICATIONS_URL = "https://finance.ec.europa.eu/publications_en"
FISMA_HOME_URL = "https://finance.ec.europa.eu/index_en"
FSB_HOME_URL = "https://www.fsb.org/"

SRB_AUTHORITY = "Single Resolution Board (SRB)"
FISMA_AUTHORITY = "European Commission DG FISMA"
FSB_AUTHORITY = "Financial Stability Board (FSB)"
AUTHORITY_ORDER = (SRB_AUTHORITY, FISMA_AUTHORITY, FSB_AUTHORITY)

DEFAULT_STATE_FILE = Path(__file__).with_name("group4_updates_state.json")
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 6_000_000
USER_AGENT = (
    "Group4-Regulatory-Updates-Monitor/1.0 "
    "(personal checker for official regulatory websites)"
)

REGULATORY_TERMS = re.compile(
    r"\b(?:"
    r"regulat(?:ion|ory)|directive|delegated act|implementing act|"
    r"consultation|supervis(?:ion|ory)|expectation|guidance|guideline|"
    r"validation|data quality|reporting|reporting framework|template|"
    r"prudential|bank(?:ing|s)|capital requirement|market risk|basel|"
    r"mica|mrel|tlac|"
    r"resolution planning|resolvability|key attributes|sound practices|"
    r"standard|rule|legal framework|banking package|sanctions?"
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


def _clean_text(parts: List[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _normalise_identity_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _normalise_url_for_identity(url: str) -> str:
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
    hostname = (parsed.hostname or "").lower()
    path = re.sub(r"/{2,}", "/", parsed.path)

    if parsed.scheme != "https" or hostname not in allowed_hosts:
        return None
    if not any(path.startswith(prefix) for prefix in required_prefixes):
        return None

    return urlunparse(
        parsed._replace(path=path, query="", fragment="")
    )


def _shorten_text(text: str, max_characters: int = 400) -> str:
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


def _classify_update(title: str, description: str, source: str) -> str:
    text = " ".join((title, description, source)).casefold()
    if "consultation" in text:
        return "Consultation paper"
    if (
        "validation" in text
        or "data quality check" in text
        or "level 3 check" in text
    ):
        return "Validation rules"
    if (
        "reporting framework" in text
        or "reporting template" in text
        or "data report" in text
        or "resolution reporting" in text
        or "reporting requirement" in text
    ):
        return "Reporting framework"
    if (
        "supervisory" in text
        or "expectation" in text
        or "guidance" in text
        or "guideline" in text
        or "sound practices" in text
        or "key attributes" in text
        or "resolvability" in text
    ):
        return "Supervisory expectations / guidance"
    return "Regulatory or policy update"


def _is_relevant_publication(item: UpdateSummary) -> bool:
    return bool(
        REGULATORY_TERMS.search(
            " ".join((item.title, item.description, item.source))
        )
    )


class SrbNewsParser(HTMLParser):
    """Extract official news cards from the SRB homepage."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: List[UpdateSummary] = []
        self.card_count = 0
        self._item: Optional[Dict[str, str]] = None
        self._article_depth = 0
        self._capture = ""
        self._parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "article":
            if self._item is not None:
                self._article_depth += 1
            elif "node--type-srb-news" in classes:
                self._item = {
                    "title": "",
                    "url": "",
                    "date": "",
                    "category": "",
                }
                self._article_depth = 1
                self.card_count += 1

        if self._item is None:
            return

        if tag == "a" and not self._item["url"]:
            url = _safe_url(
                self.base_url,
                attributes.get("href", ""),
                {"srb.europa.eu", "www.srb.europa.eu"},
                ("/en/content/",),
            )
            if url:
                self._item["url"] = url
                self._capture = "title"
                self._parts = []
        elif tag == "time" and not self._item["date"]:
            self._item["date"] = _normalise_date(
                attributes.get("datetime", "")
            )
            if not self._item["date"]:
                self._capture = "date"
                self._parts = []
        elif tag == "div" and "srb-news__srb-news-category" in classes:
            self._capture = "category"
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._item is None:
            return

        if tag == "a" and self._capture == "title":
            self._item["title"] = _clean_text(self._parts)
            self._capture = ""
        elif tag == "time" and self._capture == "date":
            self._item["date"] = _normalise_date(_clean_text(self._parts))
            self._capture = ""
        elif tag == "div" and self._capture == "category":
            self._item["category"] = _clean_text(self._parts)
            self._capture = ""

        if tag == "article":
            self._article_depth -= 1
            if self._article_depth == 0:
                item = self._item
                if all(item[key] for key in ("title", "url", "date")):
                    update = UpdateSummary(
                        title=item["title"],
                        url=item["url"],
                        authority=SRB_AUTHORITY,
                        source=item["category"] or "News",
                        update_type=_classify_update(
                            item["title"], "", item["category"]
                        ),
                        issued_date=item["date"],
                    )
                    self.items.append(update)
                self._item = None
                self._capture = ""


class SrbConsultationsParser(HTMLParser):
    """Extract SRB public consultation cards."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: List[UpdateSummary] = []
        self._item: Optional[Dict[str, str]] = None
        self._article_depth = 0
        self._capture = ""
        self._parts: List[str] = []
        self._description_depth = 0

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "article":
            if self._item is not None:
                self._article_depth += 1
            elif "node--type-srb-public-consultation" in classes:
                self._item = {
                    "title": "",
                    "url": "",
                    "date": "",
                    "description": "",
                }
                self._article_depth = 1

        if self._item is None:
            return

        if tag == "h3" and not self._item["title"]:
            self._capture = "title"
            self._parts = []
        elif tag == "time" and not self._item["date"]:
            self._item["date"] = _normalise_date(
                attributes.get("datetime", "")
            )
            if not self._item["date"]:
                self._capture = "date"
                self._parts = []

        if tag == "div":
            if self._description_depth:
                self._description_depth += 1
            elif "srb-public-consultation__body" in classes:
                self._description_depth = 1
                self._capture = "description"
                self._parts = []

        if tag == "a":
            url = _safe_url(
                self.base_url,
                attributes.get("href", ""),
                {"srb.europa.eu", "www.srb.europa.eu"},
                ("/en/content/",),
            )
            if url:
                self._item["url"] = url

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._item is None:
            return

        if tag == "h3" and self._capture == "title":
            self._item["title"] = _clean_text(self._parts)
            self._capture = ""
        elif tag == "time" and self._capture == "date":
            self._item["date"] = _normalise_date(_clean_text(self._parts))
            self._capture = ""

        if tag == "div" and self._description_depth:
            self._description_depth -= 1
            if self._description_depth == 0:
                self._item["description"] = _shorten_text(
                    _clean_text(self._parts)
                )
                self._capture = ""

        if tag == "article":
            self._article_depth -= 1
            if self._article_depth == 0:
                item = self._item
                if all(
                    item[key]
                    for key in ("title", "url", "date", "description")
                ):
                    self.items.append(
                        UpdateSummary(
                            title=item["title"],
                            url=item["url"],
                            authority=SRB_AUTHORITY,
                            source="Public consultation",
                            update_type="Consultation paper",
                            issued_date=item["date"],
                            description=item["description"],
                        )
                    )
                self._item = None
                self._capture = ""


class SrbReportingParser(HTMLParser):
    """Extract dated SRB reporting, template and validation files."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: List[UpdateSummary] = []
        self._in_section = False
        self._section_div_depth = 0
        self._heading = ""
        self._description = ""
        self._resources: List[Tuple[str, str]] = []
        self._capture = ""
        self._parts: List[str] = []
        self._paragraph_depth = 0
        self._resource_url = ""

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "div":
            if self._in_section:
                self._section_div_depth += 1
            elif "paragraph--type--srb-rich-text" in classes:
                self._in_section = True
                self._section_div_depth = 1
                self._heading = ""
                self._description = ""
                self._resources = []

        if not self._in_section:
            return

        if tag == "h2" and not self._heading:
            self._capture = "heading"
            self._parts = []
        elif tag == "p" and not self._description:
            self._paragraph_depth = 1
            self._capture = "description"
            self._parts = []
        elif tag == "p" and self._paragraph_depth:
            self._paragraph_depth += 1

        if tag == "a":
            url = _safe_url(
                self.base_url,
                attributes.get("href", ""),
                {"srb.europa.eu", "www.srb.europa.eu"},
                ("/system/files/media/document/",),
            )
            if url:
                self._resource_url = url
                self._capture = "resource"
                self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_section:
            return

        if tag == "h2" and self._capture == "heading":
            self._heading = _clean_text(self._parts)
            self._capture = ""
        elif tag == "a" and self._capture == "resource":
            title = _clean_text(self._parts)
            if title and self._resource_url:
                self._resources.append((title, self._resource_url))
            self._resource_url = ""
            self._capture = ""
        elif tag == "p" and self._paragraph_depth:
            self._paragraph_depth -= 1
            if self._paragraph_depth == 0 and self._capture == "description":
                self._description = _shorten_text(_clean_text(self._parts))
                self._capture = ""

        if tag == "div":
            self._section_div_depth -= 1
            if self._section_div_depth == 0:
                self._finish_section()

    def _finish_section(self) -> None:
        for title, url in self._resources:
            issued_date = _normalise_date(url)
            if not issued_date:
                continue
            if "data quality" in self._heading.casefold():
                update_type = "Validation rules"
                source = "Resolution reporting - data quality"
                description = self._description
            else:
                update_type = "Reporting framework"
                source = "Resolution reporting"
                context = self._description or (
                    "This file is part of the SRB's official resolution-"
                    "reporting framework."
                )
                description = _shorten_text(
                    "The SRB published this file as part of its current "
                    f"resolution-reporting framework. {context}"
                )
            self.items.append(
                UpdateSummary(
                    title=title,
                    url=url,
                    authority=SRB_AUTHORITY,
                    source=source,
                    update_type=update_type,
                    issued_date=issued_date,
                    description=description,
                )
            )
        self._in_section = False
        self._heading = ""
        self._description = ""
        self._resources = []
        self._capture = ""


class EclContentItemsParser(HTMLParser):
    """Extract cards used on European Commission list pages."""

    def __init__(
        self,
        base_url: str,
        consultation_mode: bool,
        article_classes: Tuple[str, ...] = ("ecl-content-item",),
        container_id: str = "",
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.consultation_mode = consultation_mode
        self.article_classes = set(article_classes)
        self.container_id = container_id
        self.items: List[UpdateSummary] = []
        self._in_container = not bool(container_id)
        self._container_div_depth = 0
        self._item: Optional[Dict[str, str]] = None
        self._article_depth = 0
        self._capture = ""
        self._parts: List[str] = []
        self._description_depth = 0
        self._meta_item_depth = 0

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "div" and self.container_id:
            if not self._in_container:
                if attributes.get("id") == self.container_id:
                    self._in_container = True
                    self._container_div_depth = 1
                else:
                    return
            else:
                self._container_div_depth += 1

        if not self._in_container:
            return

        if tag == "article":
            if self._item is not None:
                self._article_depth += 1
            elif self.article_classes.intersection(classes):
                self._item = {
                    "title": "",
                    "url": "",
                    "date": "",
                    "description": "",
                    "publication_type": "",
                }
                self._article_depth = 1

        if self._item is None:
            return

        if tag == "li" and "ecl-content-block__primary-meta-item" in classes:
            self._meta_item_depth = 1
            if not self._item["publication_type"]:
                self._capture = "publication_type"
                self._parts = []
        elif tag == "li" and self._meta_item_depth:
            self._meta_item_depth += 1

        if tag == "time" and not self._item["date"]:
            self._item["date"] = _normalise_date(
                attributes.get("datetime", "")
            )
            if not self._item["date"]:
                self._capture = "date"
                self._parts = []

        if tag == "a" and (
            "data-ecl-title-link" in attributes
            or "ecl-link--standalone" in classes
        ):
            url = _safe_url(
                self.base_url,
                attributes.get("href", ""),
                {
                    "finance.ec.europa.eu",
                    "ec.europa.eu",
                    "commission.europa.eu",
                },
            )
            if url and not self._item["url"]:
                self._item["url"] = url
                self._capture = "title"
                self._parts = []

        if tag == "div":
            if self._description_depth:
                self._description_depth += 1
            elif "ecl-content-block__description" in classes:
                self._description_depth = 1
                self._capture = "description"
                self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_container:
            return

        if self._item is None:
            if tag == "div" and self.container_id:
                self._container_div_depth -= 1
                if self._container_div_depth == 0:
                    self._in_container = False
            return

        if tag == "a" and self._capture == "title":
            self._item["title"] = _clean_text(self._parts)
            self._capture = ""
        elif tag == "time" and self._capture == "date":
            self._item["date"] = _normalise_date(_clean_text(self._parts))
            self._capture = ""

        if tag == "li" and self._meta_item_depth:
            self._meta_item_depth -= 1
            if (
                self._meta_item_depth == 0
                and self._capture == "publication_type"
            ):
                value = _clean_text(self._parts)
                if value and not _normalise_date(value):
                    self._item["publication_type"] = value
                self._capture = ""

        if tag == "div" and self._description_depth:
            self._description_depth -= 1
            if self._description_depth == 0:
                self._item["description"] = _shorten_text(
                    _clean_text(self._parts)
                )
                self._capture = ""

        if tag == "article":
            self._article_depth -= 1
            if self._article_depth == 0:
                item = self._item
                if all(item[key] for key in ("title", "url", "date")):
                    source = (
                        "Consultation"
                        if self.consultation_mode
                        else item["publication_type"] or "Publication"
                    )
                    update_type = (
                        "Consultation paper"
                        if self.consultation_mode
                        else _classify_update(
                            item["title"], item["description"], source
                        )
                    )
                    self.items.append(
                        UpdateSummary(
                            title=item["title"],
                            url=item["url"],
                            authority=FISMA_AUTHORITY,
                            source=source,
                            update_type=update_type,
                            issued_date=item["date"],
                            description=item["description"],
                        )
                    )
                self._item = None
                self._capture = ""

        if tag == "div" and self.container_id:
            self._container_div_depth -= 1
            if self._container_div_depth == 0:
                self._in_container = False


class FsbHomepageParser(HTMLParser):
    """Extract the FSB homepage's Publications and Consultations tabs."""

    SECTIONS = {
        "publications": "Policy publication",
        "consultations": "Consultation",
    }

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: List[UpdateSummary] = []
        self.found_sections: Set[str] = set()
        self._section = ""
        self._section_depth = 0
        self._item: Optional[Dict[str, str]] = None
        self._item_div_depth = 0
        self._capture = ""
        self._parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
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
                self.base_url,
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


class MetaDescriptionParser(HTMLParser):
    """Read an official page's concise metadata and substantive paragraphs."""

    GENERIC_DESCRIPTIONS = {
        "engagement and consultations",
        "european commission - have your say",
        "promoting global financial stability through strong financial sector policies",
    }

    def __init__(self, title: str) -> None:
        super().__init__(convert_charrefs=True)
        self.title = _normalise_identity_text(title)
        self.meta_descriptions: List[str] = []
        self.paragraphs: List[str] = []
        self._in_main = False
        self._main_depth = 0
        self._capture_paragraph = False
        self._paragraph_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
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

        if tag == "main":
            if self._in_main:
                self._main_depth += 1
            else:
                self._in_main = True
                self._main_depth = 1
        elif self._in_main and tag in {"p", "blockquote"}:
            if not self._capture_paragraph:
                self._capture_paragraph = True
                self._paragraph_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_paragraph:
            self._paragraph_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "blockquote"} and self._capture_paragraph:
            text = _clean_text(self._paragraph_parts)
            if len(text) >= 50:
                self.paragraphs.append(text)
            self._capture_paragraph = False

        if tag == "main" and self._in_main:
            self._main_depth -= 1
            if self._main_depth == 0:
                self._in_main = False

    def result(self) -> str:
        for description in self.meta_descriptions:
            normalised = _normalise_identity_text(description)
            if (
                len(description) >= 50
                and normalised != self.title
                and not self.title.startswith(normalised)
                and not (
                    normalised.startswith(self.title)
                    and len(normalised) <= len(self.title) + 20
                )
                and normalised not in self.GENERIC_DESCRIPTIONS
            ):
                return _shorten_text(description)
        for paragraph in self.paragraphs:
            normalised = _normalise_identity_text(paragraph)
            if normalised != self.title:
                return _shorten_text(paragraph)
        return ""


class SrbNewsDetailParser(HTMLParser):
    """Extract a useful paragraph from the full SRB news body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: List[str] = []
        self._body_depth = 0
        self._capture = False
        self._parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "div":
            if self._body_depth:
                self._body_depth += 1
            elif "srb-news__body" in _classes(attributes):
                self._body_depth = 1
        elif self._body_depth and tag == "p" and not self._capture:
            self._capture = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._capture:
            value = _clean_text(self._parts)
            if value:
                self.paragraphs.append(value)
            self._capture = False
        if tag == "div" and self._body_depth:
            self._body_depth -= 1

    def result(self) -> str:
        for paragraph in self.paragraphs:
            if len(paragraph) >= 80:
                return _shorten_text(paragraph)
        if self.paragraphs:
            return _shorten_text(" ".join(self.paragraphs[:3]))
        return ""


def fetch_text(url: str, timeout: float) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
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


def _unique_items(items: List[UpdateSummary]) -> List[UpdateSummary]:
    unique: Dict[str, UpdateSummary] = {}
    for item in items:
        existing = unique.get(item.identifier)
        if existing is None:
            unique[item.identifier] = item
        elif (
            item.update_type == "Consultation paper"
            and existing.update_type != "Consultation paper"
        ):
            unique[item.identifier] = item
        elif (
            item.description
            and not existing.description
        ):
            unique[item.identifier] = item
    return list(unique.values())


def parse_srb_news(html: str) -> List[UpdateSummary]:
    parser = SrbNewsParser(SRB_HOME_URL)
    parser.feed(html)
    parser.close()
    if parser.card_count == 0:
        raise MonitorError(
            "Could not find SRB news cards. The homepage layout may have changed."
        )
    return [
        item
        for item in _unique_items(parser.items)
        if item.source.casefold() == "news"
        and REGULATORY_TERMS.search(item.title)
    ]


def parse_srb_consultations(html: str) -> List[UpdateSummary]:
    parser = SrbConsultationsParser(SRB_CONSULTATIONS_URL)
    parser.feed(html)
    parser.close()
    items = _unique_items(parser.items)
    if not items:
        raise MonitorError(
            "Could not find complete SRB consultation cards. "
            "The page layout may have changed."
        )
    return items


def parse_srb_reporting(html: str) -> List[UpdateSummary]:
    parser = SrbReportingParser(SRB_REPORTING_URL)
    parser.feed(html)
    parser.close()
    items = _unique_items(parser.items)
    if not items:
        raise MonitorError(
            "Could not find dated SRB reporting or validation files. "
            "The page layout may have changed."
        )
    return items


def parse_fisma_consultations(html: str) -> List[UpdateSummary]:
    parser = EclContentItemsParser(
        FISMA_CONSULTATIONS_URL, consultation_mode=True
    )
    parser.feed(html)
    parser.close()
    items = _unique_items(parser.items)
    if not items:
        raise MonitorError(
            "Could not find DG FISMA consultation cards. "
            "The page layout may have changed."
        )
    return items


def parse_fisma_publications(html: str) -> List[UpdateSummary]:
    parser = EclContentItemsParser(
        FISMA_PUBLICATIONS_URL, consultation_mode=False
    )
    parser.feed(html)
    parser.close()
    items = [item for item in _unique_items(parser.items) if _is_relevant_publication(item)]
    if not items:
        raise MonitorError(
            "Could not find relevant DG FISMA publication cards. "
            "The page layout may have changed."
        )
    return items


def parse_fisma_homepage(html: str) -> List[UpdateSummary]:
    parser = EclContentItemsParser(
        FISMA_HOME_URL,
        consultation_mode=False,
        article_classes=("ecl-card",),
        container_id="block-news",
    )
    parser.feed(html)
    parser.close()
    items = [
        item
        for item in _unique_items(parser.items)
        if _is_relevant_publication(item)
    ]
    if not items:
        raise MonitorError(
            "Could not find relevant DG FISMA homepage news cards. "
            "The homepage layout may have changed."
        )
    return items


def parse_fsb_homepage(html: str) -> List[UpdateSummary]:
    parser = FsbHomepageParser(FSB_HOME_URL)
    parser.feed(html)
    parser.close()
    if parser.found_sections != set(FsbHomepageParser.SECTIONS):
        raise MonitorError(
            "Could not find both FSB Publications and Consultations sections. "
            "The homepage layout may have changed."
        )
    items = _unique_items(parser.items)
    if not items:
        raise MonitorError(
            "Could not find complete FSB publication cards. "
            "The homepage layout may have changed."
        )
    return items


def _external_consultation_description(title: str) -> str:
    consultation_kind = (
        "targeted consultation"
        if title.casefold().startswith("targeted")
        else "public consultation"
    )
    topic = re.sub(
        r"^(?:targeted|public)\s+consultation\s+on\s+(?:the\s+)?",
        "",
        title,
        flags=re.IGNORECASE,
    ).rstrip(".")
    return (
        f"The European Commission opened this {consultation_kind} to collect "
        f"stakeholder feedback on {topic}."
    )


def _description_from_detail(
    item: UpdateSummary,
    html: str,
) -> str:
    if item.authority == SRB_AUTHORITY:
        parser = SrbNewsDetailParser()
        parser.feed(html)
        parser.close()
        description = parser.result()
    else:
        parser = MetaDescriptionParser(item.title)
        parser.feed(html)
        parser.close()
        description = parser.result()

    if not description:
        raise MonitorError(
            f"The official page did not provide a usable description: {item.url}"
        )
    return description


def _description_is_useful(title: str, description: str) -> bool:
    if len(description.strip()) < 50:
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

    if not isinstance(state, dict) or not isinstance(state.get("updates"), list):
        raise MonitorError(f"Saved update history has an invalid format: {path}")
    for record in state["updates"]:
        if not isinstance(record, dict):
            raise MonitorError(f"Saved update history is invalid: {path}")
        if not isinstance(record.get("identifier"), str):
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


def check_for_updates(
    state_path: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    fetcher: Optional[Callable[[str, float], str]] = None,
) -> List[RegulatoryUpdate]:
    if timeout <= 0:
        raise MonitorError("Timeout must be greater than zero.")
    get_page = fetcher or fetch_text

    # Fetch every listing before changing the saved history. If any official
    # source fails, the run stops without incorrectly marking items as seen.
    srb_home_html = get_page(SRB_HOME_URL, timeout)
    srb_consultations_html = get_page(SRB_CONSULTATIONS_URL, timeout)
    srb_reporting_html = get_page(SRB_REPORTING_URL, timeout)
    fisma_consultations_html = get_page(FISMA_CONSULTATIONS_URL, timeout)
    fisma_publications_html = get_page(FISMA_PUBLICATIONS_URL, timeout)
    fisma_home_html = get_page(FISMA_HOME_URL, timeout)
    fsb_home_html = get_page(FSB_HOME_URL, timeout)

    current_items: List[UpdateSummary] = []
    current_items.extend(parse_srb_news(srb_home_html))
    current_items.extend(parse_srb_consultations(srb_consultations_html))
    current_items.extend(parse_srb_reporting(srb_reporting_html))
    current_items.extend(parse_fisma_consultations(fisma_consultations_html))
    current_items.extend(parse_fisma_publications(fisma_publications_html))
    current_items.extend(parse_fisma_homepage(fisma_home_html))
    current_items.extend(parse_fsb_homepage(fsb_home_html))
    current_items = _unique_items(current_items)

    state = load_state(state_path)
    seen_identifiers = {
        record["identifier"]
        for record in state["updates"]
        if isinstance(record, dict)
        and isinstance(record.get("identifier"), str)
    }

    new_updates: List[RegulatoryUpdate] = []
    for item in current_items:
        if item.identifier in seen_identifiers:
            continue

        description = (
            item.description
            if _description_is_useful(item.title, item.description)
            else ""
        )
        if not description:
            hostname = (urlparse(item.url).hostname or "").casefold()
            if (
                item.authority == FISMA_AUTHORITY
                and hostname != "finance.ec.europa.eu"
                and item.update_type == "Consultation paper"
            ):
                description = _external_consultation_description(item.title)
            else:
                detail_html = get_page(item.url, timeout)
                description = _description_from_detail(item, detail_html)

        if not item.issued_date or not description:
            raise MonitorError(
                f"An update was missing its date or description: {item.url}"
            )
        update_type = _classify_update(
            item.title, description, item.source
        )
        if item.update_type in {
            "Consultation paper",
            "Validation rules",
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
            "Print unseen Group 4 regulatory updates, grouped as SRB, "
            "European Commission DG FISMA, then FSB."
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
