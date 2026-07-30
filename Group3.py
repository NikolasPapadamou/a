#!/usr/bin/env python3
"""Print unseen updates from official financial regulatory websites."""

from __future__ import annotations

import argparse
import html as html_module
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


SOURCE_URL = "https://www.fca.org.uk/"
PUBLICATIONS_URL = "https://www.fca.org.uk/publications"
CBC_HOME_URL = "https://www.centralbank.cy/en/home"
BOG_HOME_URL = "https://www.bankofgreece.gr/en"
MFSA_UPDATES_URL = (
    "https://www.mfsa.mt/news/international-regulatory-updates/"
)
MFSA_FEED_URL = "https://www.mfsa.mt/feed/"
BOE_PRA_URL = "https://www.bankofengland.co.uk/prudential-regulation"
FCA_AUTHORITY = "Financial Conduct Authority (FCA)"
CBC_AUTHORITY = "Central Bank of Cyprus (CBC)"
BOG_AUTHORITY = "Bank of Greece (BoG)"
MFSA_AUTHORITY = "Malta Financial Services Authority (MFSA)"
BOE_PRA_AUTHORITY = (
    "Bank of England - Prudential Regulation Authority (PRA)"
)
AUTHORITY_ORDER = (
    FCA_AUTHORITY,
    CBC_AUTHORITY,
    BOG_AUTHORITY,
    MFSA_AUTHORITY,
    BOE_PRA_AUTHORITY,
)
DEFAULT_STATE_FILE = Path(__file__).with_name("fca_updates_state.json")
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 5_000_000
RECENT_LOOKBACK_DAYS = 180
USER_AGENT = (
    "Regulatory-Updates-Monitor/5.0 "
    "(personal checker for official regulatory websites)"
)

REGULATORY_TERMS = re.compile(
    r"\b(?:regulat(?:ion|ory)|rules?|requirements?|consultation|"
    r"policy statement|guidance|guidelines?|supervis(?:ion|ory)|"
    r"expectations?|prudential|resolution|resolvability|reporting|"
    r"reporting framework|validation|data quality|assessment|"
    r"financial stability|systemic risk|capital|liquidity|"
    r"operational resilience|market risk|Basel|Solvency|AIFM|"
    r"remuneration|short selling|fees? and levies|"
    r"technical standards?|regulatory framework)\b",
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
    issued_date: str = ""
    description: str = ""


@dataclass(frozen=True)
class RegulatoryUpdate:
    title: str
    description: str
    issued_date: str
    url: str
    authority: str
    source: str
    update_type: str


@dataclass(frozen=True)
class DetailMetadata:
    description: str
    issued_date: str


def _classes(attributes: Dict[str, str]) -> Set[str]:
    return set(attributes.get("class", "").split())


def _clean_text(parts: List[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _safe_site_url(
    base_url: str,
    href: str,
    allowed_hosts: Set[str],
    required_path_prefix: str,
    keep_query: bool = False,
) -> Optional[str]:
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)

    if parsed.scheme != "https":
        return None
    if (parsed.hostname or "").lower() not in allowed_hosts:
        return None
    normalised_path = re.sub(r"/{2,}", "/", parsed.path)
    if not normalised_path.startswith(required_path_prefix):
        return None

    return urlunparse(
        parsed._replace(
            path=normalised_path,
            query=parsed.query if keep_query else "",
            fragment="",
        )
    )


def _safe_fca_url(
    base_url: str, href: str, required_path_prefix: str
) -> Optional[str]:
    return _safe_site_url(
        base_url=base_url,
        href=href,
        allowed_hosts={"fca.org.uk", "www.fca.org.uk"},
        required_path_prefix=required_path_prefix,
    )


class LatestNewsParser(HTMLParser):
    """Extract the cards inside the FCA homepage's Latest news section."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: List[UpdateSummary] = []

        self._in_latest_section = False
        self._nested_section_depth = 0
        self._item: Optional[Dict[str, str]] = None
        self._item_div_depth = 0
        self._capture_title = False
        self._title_parts: List[str] = []
        self._capture_date = False
        self._date_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}

        if tag == "section":
            if not self._in_latest_section:
                if "latest-news-cards" in _classes(attributes):
                    self._in_latest_section = True
                    self._nested_section_depth = 1
                return
            self._nested_section_depth += 1

        if not self._in_latest_section:
            return

        if tag == "div":
            if self._item is not None:
                self._item_div_depth += 1
            elif "latest-news__item" in _classes(attributes):
                self._item = {"title": "", "date": "", "url": ""}
                self._item_div_depth = 1

        if self._item is None:
            return

        if tag == "a" and not self._item["url"]:
            url = _safe_fca_url(
                self.base_url,
                attributes.get("href", ""),
                required_path_prefix="/news/",
            )
            if url is not None:
                self._item["url"] = url
                self._capture_title = True
                self._title_parts = []

        if tag == "time":
            self._capture_date = True
            self._date_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title_parts.append(data)
        if self._capture_date:
            self._date_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_latest_section:
            return

        if tag == "a" and self._capture_title:
            self._capture_title = False
            if self._item is not None:
                self._item["title"] = _clean_text(self._title_parts)

        if tag == "time" and self._capture_date:
            self._capture_date = False
            if self._item is not None:
                self._item["date"] = _clean_text(self._date_parts)

        if tag == "div" and self._item is not None:
            self._item_div_depth -= 1
            if self._item_div_depth == 0:
                title = self._item["title"]
                issued_date = self._item["date"]
                url = self._item["url"]
                if title and issued_date and url:
                    self.items.append(
                        UpdateSummary(
                            title=title,
                            url=url,
                            authority=FCA_AUTHORITY,
                            source="Latest news",
                            issued_date=issued_date,
                        )
                    )
                self._item = None
                self._capture_title = False
                self._capture_date = False

        if tag == "section":
            self._nested_section_depth -= 1
            if self._nested_section_depth == 0:
                self._in_latest_section = False


class LatestPublicationsParser(HTMLParser):
    """Extract consultations and policy/guidance from the Publications page."""

    SOURCE_HEADINGS = {
        "latest consultations": "Consultation",
        "latest policy and guidance": "Policy and guidance",
    }

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: List[UpdateSummary] = []

        self._in_publications_section = False
        self._nested_section_depth = 0
        self._capture_heading = False
        self._heading_parts: List[str] = []
        self._current_source = ""
        self._item: Optional[Dict[str, str]] = None
        self._item_div_depth = 0
        self._capture_title = False
        self._title_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "section":
            if not self._in_publications_section:
                if {"two-column-content", "fca-about"}.issubset(classes):
                    self._in_publications_section = True
                    self._nested_section_depth = 1
                return
            self._nested_section_depth += 1

        if not self._in_publications_section:
            return

        if tag == "h2" and "content-block__title" in classes:
            self._capture_heading = True
            self._heading_parts = []

        if tag == "div":
            if self._item is not None:
                self._item_div_depth += 1
            elif "link-and-title" in classes and self._current_source:
                self._item = {"title": "", "url": ""}
                self._item_div_depth = 1

        if self._item is not None and tag == "a" and not self._item["url"]:
            url = _safe_fca_url(
                self.base_url,
                attributes.get("href", ""),
                required_path_prefix="/publications/",
            )
            if url is not None and urlparse(url).path != "/publications/search-results":
                self._item["url"] = url
                self._capture_title = True
                self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_heading:
            self._heading_parts.append(data)
        if self._capture_title:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_publications_section:
            return

        if tag == "h2" and self._capture_heading:
            self._capture_heading = False
            heading = _clean_text(self._heading_parts).lower()
            self._current_source = self.SOURCE_HEADINGS.get(heading, "")

        if tag == "a" and self._capture_title:
            self._capture_title = False
            if self._item is not None:
                self._item["title"] = _clean_text(self._title_parts)

        if tag == "div" and self._item is not None:
            self._item_div_depth -= 1
            if self._item_div_depth == 0:
                title = self._item["title"]
                url = self._item["url"]
                if title and url:
                    self.items.append(
                        UpdateSummary(
                            title=title,
                            url=url,
                            authority=FCA_AUTHORITY,
                            source=self._current_source,
                        )
                    )
                self._item = None
                self._capture_title = False

        if tag == "section":
            self._nested_section_depth -= 1
            if self._nested_section_depth == 0:
                self._in_publications_section = False


class CbcAnnouncementsParser(HTMLParser):
    """Extract announcement cards from the Central Bank of Cyprus homepage."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: List[UpdateSummary] = []

        self._in_announcements = False
        self._nested_section_depth = 0
        self._item: Optional[Dict[str, str]] = None
        self._item_div_depth = 0
        self._capture_title = False
        self._title_parts: List[str] = []
        self._capture_date = False
        self._date_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "section":
            if not self._in_announcements:
                if attributes.get("id") == "announcements_home":
                    self._in_announcements = True
                    self._nested_section_depth = 1
                return
            self._nested_section_depth += 1

        if not self._in_announcements:
            return

        if tag == "div":
            if self._item is not None:
                self._item_div_depth += 1
            elif "announcement-list" in classes:
                self._item = {"title": "", "date": "", "url": ""}
                self._item_div_depth = 1

        if self._item is None:
            return

        if tag == "h5":
            self._capture_title = True
            self._title_parts = []

        if tag == "span" and "date" in classes:
            self._capture_date = True
            self._date_parts = []

        if tag == "a" and not self._item["url"]:
            url = _safe_site_url(
                base_url=self.base_url,
                href=attributes.get("href", ""),
                allowed_hosts={"centralbank.cy", "www.centralbank.cy"},
                required_path_prefix="/en/announcements/",
            )
            if url is not None:
                self._item["url"] = url

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title_parts.append(data)
        if self._capture_date:
            self._date_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_announcements:
            return

        if tag == "h5" and self._capture_title:
            self._capture_title = False
            if self._item is not None:
                self._item["title"] = _clean_text(self._title_parts)

        if tag == "span" and self._capture_date:
            self._capture_date = False
            if self._item is not None:
                self._item["date"] = _clean_text(self._date_parts)

        if tag == "div" and self._item is not None:
            self._item_div_depth -= 1
            if self._item_div_depth == 0:
                title = self._item["title"]
                issued_date = _normalise_date(self._item["date"])
                url = self._item["url"]
                if title and issued_date and url:
                    self.items.append(
                        UpdateSummary(
                            title=title,
                            url=url,
                            authority=CBC_AUTHORITY,
                            source="Announcements",
                            issued_date=issued_date,
                        )
                    )
                self._item = None
                self._capture_title = False
                self._capture_date = False

        if tag == "section":
            self._nested_section_depth -= 1
            if self._nested_section_depth == 0:
                self._in_announcements = False


class CbcArticleParser(HTMLParser):
    """Extract the first substantive paragraph from a CBC announcement."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.description = ""
        self.alternate_language_url = ""
        self._in_article = False
        self._nested_section_depth = 0
        self._in_body = False
        self._body_div_depth = 0
        self._capture_paragraph = False
        self._paragraph_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "section":
            if not self._in_article:
                if attributes.get("id") == "generic_article":
                    self._in_article = True
                    self._nested_section_depth = 1
                return
            self._nested_section_depth += 1

        if not self._in_article or self.description:
            return

        if tag == "div":
            if self._in_body:
                self._body_div_depth += 1
            elif "article-image" in classes:
                self._in_body = True
                self._body_div_depth = 1

        if self._in_body and tag == "p" and not self._capture_paragraph:
            self._capture_paragraph = True
            self._paragraph_parts = []

        if self._in_body and tag == "a" and not self.alternate_language_url:
            alternate_url = _safe_site_url(
                base_url=self.base_url,
                href=attributes.get("href", ""),
                allowed_hosts={"centralbank.cy", "www.centralbank.cy"},
                required_path_prefix="/el/announcements/",
            )
            if alternate_url is not None:
                self.alternate_language_url = alternate_url

    def handle_data(self, data: str) -> None:
        if self._capture_paragraph:
            self._paragraph_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_article:
            return

        if tag == "p" and self._capture_paragraph:
            self._capture_paragraph = False
            paragraph = _clean_text(self._paragraph_parts)
            if paragraph:
                self.description = _shorten_text(paragraph)

        if tag == "div" and self._in_body:
            self._body_div_depth -= 1
            if self._body_div_depth == 0:
                self._in_body = False

        if tag == "section":
            self._nested_section_depth -= 1
            if self._nested_section_depth == 0:
                self._in_article = False


class BogHomepageParser(HTMLParser):
    """Extract the public News cards from the Bank of Greece homepage."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: List[UpdateSummary] = []

        self._in_news_section = False
        self._nested_section_depth = 0
        self._in_all_news = False
        self._all_news_div_depth = 0
        self._item: Optional[Dict[str, str]] = None
        self._item_root_tag = ""
        self._item_depth = 0
        self._capture_title = False
        self._title_parts: List[str] = []
        self._capture_date = False
        self._date_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "section":
            if not self._in_news_section:
                if "newsTabsSection" in classes:
                    self._in_news_section = True
                    self._nested_section_depth = 1
                return
            self._nested_section_depth += 1

        if not self._in_news_section:
            return

        if tag == "div":
            if not self._in_all_news:
                if attributes.get("id") == "all-news":
                    self._in_all_news = True
                    self._all_news_div_depth = 1
                return

            self._all_news_div_depth += 1

        if not self._in_all_news:
            return

        if tag in {"div", "li"}:
            if self._item is not None:
                if tag == self._item_root_tag:
                    self._item_depth += 1
            elif "newsTabs__item__art" in classes:
                self._item = {"title": "", "date": "", "url": ""}
                self._item_root_tag = tag
                self._item_depth = 1

        if (
            self._item is not None
            and tag == "div"
            and "newsTabs__item__art__date" in classes
        ):
            self._capture_date = True
            self._date_parts = []

        if self._item is None:
            return

        if tag == "a" and not self._item["url"]:
            url = _safe_site_url(
                base_url=self.base_url,
                href=attributes.get("href", ""),
                allowed_hosts={"bankofgreece.gr", "www.bankofgreece.gr"},
                required_path_prefix=(
                    "/en/news-and-media/press-office/news-list/news"
                ),
                keep_query=True,
            )
            if url is not None and re.search(
                r"(?:^|&)announcement=[0-9a-f-]+(?:&|$)",
                urlparse(url).query,
                flags=re.IGNORECASE,
            ):
                self._item["url"] = url
                self._capture_title = True
                self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title_parts.append(data)
        if self._capture_date:
            self._date_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_news_section:
            return

        if tag == "a" and self._capture_title:
            self._capture_title = False
            if self._item is not None:
                self._item["title"] = _clean_text(self._title_parts)

        if tag == "div" and self._capture_date:
            self._capture_date = False
            if self._item is not None:
                self._item["date"] = _clean_text(self._date_parts)

        if (
            self._item is not None
            and tag == self._item_root_tag
        ):
            self._item_depth -= 1
            if self._item_depth == 0:
                title = self._item["title"]
                issued_date = _normalise_date(self._item["date"])
                url = self._item["url"]
                if title and issued_date and url:
                    self.items.append(
                        UpdateSummary(
                            title=title,
                            url=url,
                            authority=BOG_AUTHORITY,
                            source="Homepage news",
                            issued_date=issued_date,
                        )
                    )
                self._item = None
                self._item_root_tag = ""
                self._capture_title = False
                self._capture_date = False

        if tag == "div" and self._in_all_news:
            self._all_news_div_depth -= 1
            if self._all_news_div_depth == 0:
                self._in_all_news = False

        if tag == "section":
            self._nested_section_depth -= 1
            if self._nested_section_depth == 0:
                self._in_news_section = False


class BogArticleParser(HTMLParser):
    """Extract the first substantive paragraph from Bank of Greece news."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.description = ""
        self._in_article_main = False
        self._article_div_depth = 0
        self._capture_metadata = False
        self._metadata_seen = False
        self._capture_paragraph = False
        self._paragraph_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "div":
            if self._in_article_main:
                self._article_div_depth += 1
            elif "articleMain" in classes:
                self._in_article_main = True
                self._article_div_depth = 1

        if not self._in_article_main or self.description:
            return

        if tag == "h3":
            self._capture_metadata = True

        if (
            self._metadata_seen
            and tag == "p"
            and not self._capture_paragraph
        ):
            self._capture_paragraph = True
            self._paragraph_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_paragraph:
            self._paragraph_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_article_main:
            return

        if tag == "h3" and self._capture_metadata:
            self._capture_metadata = False
            self._metadata_seen = True

        if tag == "p" and self._capture_paragraph:
            self._capture_paragraph = False
            paragraph = _clean_text(self._paragraph_parts).lstrip("-–—• ")
            if paragraph:
                self.description = _shorten_text(paragraph)

        if tag == "div":
            self._article_div_depth -= 1
            if self._article_div_depth == 0:
                self._in_article_main = False


class MfsaUpdatesParser(HTMLParser):
    """Extract cards from MFSA's International Regulatory Updates page."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: List[UpdateSummary] = []

        self._item: Optional[Dict[str, str]] = None
        self._item_div_depth = 0
        self._capture_date = False
        self._date_parts: List[str] = []
        self._capture_title = False
        self._title_parts: List[str] = []
        self._capture_excerpt = False
        self._excerpt_div_depth = 0
        self._excerpt_parts: List[str] = []
        self._in_resource_item = False
        self._resource_li_depth = 0
        self._capture_resource = False
        self._resource_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "div":
            if self._item is not None:
                self._item_div_depth += 1
                if self._capture_excerpt:
                    self._excerpt_div_depth += 1
            elif "single-news-item" in classes:
                self._item = {
                    "title": "",
                    "date": "",
                    "url": "",
                    "excerpt": "",
                    "resource": "",
                }
                self._item_div_depth = 1

        if self._item is None:
            return

        if tag == "div" and "date-published" in classes:
            self._capture_date = True
            self._date_parts = []

        if (
            tag == "a"
            and "title-link" in classes
            and not self._item["url"]
        ):
            url = _safe_site_url(
                base_url=self.base_url,
                href=attributes.get("href", ""),
                allowed_hosts={"mfsa.mt", "www.mfsa.mt"},
                required_path_prefix="/news-item/",
            )
            if url is not None:
                self._item["url"] = url
                self._capture_title = True
                self._title_parts = []

        if tag == "div" and "news-item-excerpt" in classes:
            self._capture_excerpt = True
            self._excerpt_div_depth = 1
            self._excerpt_parts = []

        if tag == "li":
            if self._in_resource_item:
                self._resource_li_depth += 1
            elif "resource-item" in classes:
                self._in_resource_item = True
                self._resource_li_depth = 1

        if (
            self._in_resource_item
            and tag == "a"
            and not self._item["resource"]
        ):
            self._capture_resource = True
            self._resource_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_date:
            self._date_parts.append(data)
        if self._capture_title:
            self._title_parts.append(data)
        if self._capture_excerpt:
            self._excerpt_parts.append(data)
        if self._capture_resource:
            self._resource_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._item is None:
            return

        if tag == "a" and self._capture_title:
            self._capture_title = False
            self._item["title"] = _clean_text(self._title_parts)

        if tag == "a" and self._capture_resource:
            self._capture_resource = False
            self._item["resource"] = _clean_text(self._resource_parts)

        if tag == "li" and self._in_resource_item:
            self._resource_li_depth -= 1
            if self._resource_li_depth == 0:
                self._in_resource_item = False

        if tag == "div" and self._capture_excerpt:
            self._excerpt_div_depth -= 1
            if self._excerpt_div_depth == 0:
                self._capture_excerpt = False
                self._item["excerpt"] = _clean_text(self._excerpt_parts)

        if tag == "div" and self._capture_date:
            self._capture_date = False
            self._item["date"] = _clean_text(self._date_parts)

        if tag == "div":
            self._item_div_depth -= 1
            if self._item_div_depth == 0:
                title = self._item["title"]
                issued_date = _normalise_date(self._item["date"])
                url = self._item["url"]
                description = self._item["excerpt"]
                if not description and self._item["resource"]:
                    description = (
                        f"MFSA links to an official "
                        f"{self._item['resource'].lower()} titled: {title}."
                    )
                if title and issued_date and url and description:
                    self.items.append(
                        UpdateSummary(
                            title=title,
                            url=url,
                            authority=MFSA_AUTHORITY,
                            source="International regulatory updates",
                            issued_date=issued_date,
                            description=_shorten_text(description),
                        )
                    )
                self._item = None
                self._capture_date = False
                self._capture_title = False
                self._capture_excerpt = False
                self._in_resource_item = False


class MfsaFeedDescriptionParser(HTMLParser):
    """Extract the first paragraph from an MFSA RSS description."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.description = ""
        self._capture_paragraph = False
        self._paragraph_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        if tag == "p" and not self.description:
            self._capture_paragraph = True
            self._paragraph_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_paragraph:
            self._paragraph_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._capture_paragraph:
            self._capture_paragraph = False
            paragraph = _clean_text(self._paragraph_parts)
            if paragraph:
                self.description = paragraph


class BoePraUpdatesParser(HTMLParser):
    """Extract open consultations and latest policy from the PRA page."""

    SOURCE_HEADINGS = {
        "open consultations and discussion papers": (
            "Open consultation or discussion paper"
        ),
        "latest policy": "Latest policy",
    }

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: List[UpdateSummary] = []
        self._current_source = ""
        self._capture_heading = False
        self._heading_parts: List[str] = []
        self._in_paragraph = False
        self._paragraph_parts: List[str] = []
        self._paragraph_url = ""
        self._paragraph_title = ""
        self._capture_title = False
        self._title_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}

        if tag == "button" and "accordion-button" in _classes(attributes):
            self._capture_heading = True
            self._heading_parts = []

        if tag == "p":
            self._in_paragraph = True
            self._paragraph_parts = []
            self._paragraph_url = ""
            self._paragraph_title = ""

        if not self._in_paragraph or tag != "a" or not self._current_source:
            return

        url = _safe_site_url(
            base_url=self.base_url,
            href=attributes.get("href", ""),
            allowed_hosts={
                "bankofengland.co.uk",
                "www.bankofengland.co.uk",
            },
            required_path_prefix="/prudential-regulation/publication/",
        )
        if url is not None and (
            not self._paragraph_url
            or (
                self._paragraph_url == url
                and not self._paragraph_title
            )
        ):
            self._paragraph_url = url
            self._capture_title = True
            self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_heading:
            self._heading_parts.append(data)
        if self._in_paragraph:
            self._paragraph_parts.append(data)
        if self._capture_title:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._capture_heading:
            self._capture_heading = False
            heading = _clean_text(self._heading_parts).lower()
            self._current_source = self.SOURCE_HEADINGS.get(heading, "")

        if tag == "a" and self._capture_title:
            self._capture_title = False
            title = _clean_text(self._title_parts)
            if title:
                self._paragraph_title = title

        if tag == "p" and self._in_paragraph:
            self._in_paragraph = False
            paragraph = _clean_text(self._paragraph_parts)
            date_match = re.search(
                r"\b\d{1,2}\s+[A-Za-z]+\s+20\d{2}\b",
                paragraph,
            )
            issued_date = (
                _normalise_date(date_match.group(0)) if date_match else ""
            )
            if (
                self._paragraph_title
                and self._paragraph_url
                and issued_date
                and self._current_source
            ):
                self.items.append(
                    UpdateSummary(
                        title=self._paragraph_title,
                        url=self._paragraph_url,
                        authority=BOE_PRA_AUTHORITY,
                        source=self._current_source,
                        issued_date=issued_date,
                    )
                )


class BoePraDetailParser(HTMLParser):
    """Collect the PRA publication date and substantive body paragraphs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.published_date = ""
        self.paragraphs: List[str] = []
        self._capture_date = False
        self._date_div_depth = 0
        self._date_parts: List[str] = []
        self._in_output = False
        self._output_div_depth = 0
        self._capture_paragraph = False
        self._paragraph_parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = _classes(attributes)

        if tag == "div":
            if self._capture_date:
                self._date_div_depth += 1
            elif "published-date" in classes:
                self._capture_date = True
                self._date_div_depth = 1
                self._date_parts = []

            if self._in_output:
                self._output_div_depth += 1
            elif attributes.get("id") == "output":
                self._in_output = True
                self._output_div_depth = 1

        if self._in_output and tag == "p" and not self._capture_paragraph:
            self._capture_paragraph = True
            self._paragraph_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_date:
            self._date_parts.append(data)
        if self._capture_paragraph:
            self._paragraph_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._capture_paragraph:
            self._capture_paragraph = False
            paragraph = _clean_text(self._paragraph_parts)
            if paragraph:
                self.paragraphs.append(paragraph)

        if tag == "div" and self._capture_date:
            self._date_div_depth -= 1
            if self._date_div_depth == 0:
                self._capture_date = False
                date_text = _clean_text(self._date_parts)
                self.published_date = re.sub(
                    r"^Published on\s+",
                    "",
                    date_text,
                    flags=re.IGNORECASE,
                )

        if tag == "div" and self._in_output:
            self._output_div_depth -= 1
            if self._output_div_depth == 0:
                self._in_output = False


class DetailMetadataParser(HTMLParser):
    """Extract the FCA-supplied description and publication date."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.description = ""
        self._fallback_description = ""
        self.published_date = ""

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        if tag != "meta":
            return

        attributes = {name.lower(): value or "" for name, value in attrs}
        content = re.sub(r"\s+", " ", attributes.get("content", "")).strip()
        if not content:
            return

        if attributes.get("name", "").lower() == "description":
            self.description = content
        elif (
            attributes.get("property", "").lower() == "og:description"
            and not self._fallback_description
        ):
            self._fallback_description = content
        elif attributes.get("property", "").lower() == "funnelback:published-date":
            self.published_date = content

    def result(self) -> str:
        return self.description or self._fallback_description


class GenericDescriptionParser(HTMLParser):
    """Collect complete explanatory paragraphs from an official detail page."""

    EXCLUDED_PREFIXES = (
        "accept additional cookies",
        "cookies on",
        "share this page",
        "subscribe",
        "the central bank of cyprus was established",
        "the bank of greece",
    )

    def __init__(self, title: str) -> None:
        super().__init__(convert_charrefs=True)
        self.title = re.sub(r"\s+", " ", title).strip().casefold()
        self.meta_descriptions: List[str] = []
        self.paragraphs: List[str] = []
        self._capture = False
        self._parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        if tag == "meta":
            name = attributes.get("name", "").casefold()
            prop = attributes.get("property", "").casefold()
            if name == "description" or prop == "og:description":
                value = re.sub(
                    r"\s+", " ", attributes.get("content", "")
                ).strip()
                if value:
                    self.meta_descriptions.append(value)
        elif tag in {"p", "blockquote"} and not self._capture:
            self._capture = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "blockquote"} and self._capture:
            self._capture = False
            value = _clean_text(self._parts)
            normalised = value.casefold()
            if (
                len(value) >= 55
                and normalised != self.title
                and not normalised.startswith(self.EXCLUDED_PREFIXES)
            ):
                self.paragraphs.append(value)

    def result(self) -> str:
        if self.paragraphs:
            return _shorten_text(self.paragraphs[0])
        for description in self.meta_descriptions:
            if _description_is_useful(self.title, description):
                return _shorten_text(description)
        return ""


def fetch_text(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = USER_AGENT,
    accepted_content_types: Optional[Set[str]] = None,
) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-GB,en;q=0.8",
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            allowed_content_types = accepted_content_types or {
                "text/html",
                "application/xhtml+xml",
            }
            if content_type not in allowed_content_types:
                raise MonitorError(
                    "Website returned unexpected content type "
                    f"{content_type!r} for {url}"
                )

            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise MonitorError(
                    f"Website response was unexpectedly large for {url}"
                )

            encoding = response.headers.get_content_charset() or "utf-8"
            try:
                return raw.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                return raw.decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise MonitorError(
            f"Website returned HTTP {exc.code} while requesting {url}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise MonitorError(f"Could not retrieve {url}: {reason}") from exc


def parse_latest_updates(html: str, base_url: str = SOURCE_URL) -> List[UpdateSummary]:
    parser = LatestNewsParser(base_url)
    parser.feed(html)
    parser.close()

    unique_items: List[UpdateSummary] = []
    seen_urls: Set[str] = set()
    for item in parser.items:
        if item.url not in seen_urls:
            unique_items.append(item)
            seen_urls.add(item.url)

    if not unique_items:
        raise MonitorError(
            "Could not find any complete items in the FCA homepage's "
            "'Latest news' section. The page layout may have changed."
        )
    return unique_items


def parse_latest_publications(
    html: str, base_url: str = PUBLICATIONS_URL
) -> List[UpdateSummary]:
    parser = LatestPublicationsParser(base_url)
    parser.feed(html)
    parser.close()

    unique_items: List[UpdateSummary] = []
    seen_urls: Set[str] = set()
    for item in parser.items:
        if item.url not in seen_urls:
            unique_items.append(item)
            seen_urls.add(item.url)

    expected_sources = set(LatestPublicationsParser.SOURCE_HEADINGS.values())
    found_sources = {item.source for item in unique_items}
    if not unique_items or found_sources != expected_sources:
        raise MonitorError(
            "Could not find complete 'Latest consultations' and "
            "'Latest policy and guidance' lists on the FCA Publications page. "
            "The page layout may have changed."
        )
    return unique_items


def parse_cbc_announcements(
    html: str, base_url: str = CBC_HOME_URL
) -> List[UpdateSummary]:
    parser = CbcAnnouncementsParser(base_url)
    parser.feed(html)
    parser.close()

    unique_items: List[UpdateSummary] = []
    seen_urls: Set[str] = set()
    for item in parser.items:
        if item.url not in seen_urls:
            unique_items.append(item)
            seen_urls.add(item.url)

    if not unique_items:
        raise MonitorError(
            "Could not find complete items in the Central Bank of Cyprus "
            "homepage's 'Announcements' section. The page layout may have changed."
        )
    return unique_items


def parse_bog_homepage(
    html: str, base_url: str = BOG_HOME_URL
) -> List[UpdateSummary]:
    parser = BogHomepageParser(base_url)
    parser.feed(html)
    parser.close()

    unique_items: List[UpdateSummary] = []
    seen_urls: Set[str] = set()
    for item in parser.items:
        if item.url not in seen_urls:
            unique_items.append(item)
            seen_urls.add(item.url)

    if not unique_items:
        raise MonitorError(
            "Could not find complete items in the Bank of Greece homepage's "
            "'News' section. The page layout may have changed."
        )
    return unique_items


def parse_mfsa_updates(
    html: str, base_url: str = MFSA_UPDATES_URL
) -> List[UpdateSummary]:
    parser = MfsaUpdatesParser(base_url)
    parser.feed(html)
    parser.close()

    unique_items: List[UpdateSummary] = []
    seen_urls: Set[str] = set()
    for item in parser.items:
        if item.url not in seen_urls:
            unique_items.append(item)
            seen_urls.add(item.url)

    if not unique_items:
        raise MonitorError(
            "Could not find complete items on MFSA's 'International "
            "Regulatory Updates' page. The page layout may have changed."
        )
    return unique_items


def parse_mfsa_feed(
    xml_text: str, base_url: str = MFSA_FEED_URL
) -> List[UpdateSummary]:
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff \t\r\n"))
    except ET.ParseError as exc:
        raise MonitorError("MFSA returned an invalid RSS feed.") from exc

    unique_items: List[UpdateSummary] = []
    seen_urls: Set[str] = set()
    for element in root.findall("./channel/item"):
        title = re.sub(
            r"\s+",
            " ",
            element.findtext("title", default=""),
        ).strip()
        url = _safe_site_url(
            base_url=base_url,
            href=element.findtext("link", default="").strip(),
            allowed_hosts={"mfsa.mt", "www.mfsa.mt"},
            required_path_prefix="/",
        )
        issued_date = _normalise_date(
            element.findtext("pubDate", default="")
        )
        description_html = element.findtext("description", default="")
        description_parser = MfsaFeedDescriptionParser()
        description_parser.feed(description_html)
        description_parser.close()
        description = description_parser.description
        if not description and title:
            description = f"MFSA published an official update titled: {title}."

        if not title or not url or not issued_date or not description:
            continue
        if url in seen_urls:
            continue

        path_part = urlparse(url).path.strip("/").split("/", 1)[0]
        source = {
            "publication": "Publication",
            "news-item": "News item",
            "events": "Event",
        }.get(path_part, "Official website feed")
        unique_items.append(
            UpdateSummary(
                title=title,
                url=url,
                authority=MFSA_AUTHORITY,
                source=source,
                issued_date=issued_date,
                description=_shorten_text(description),
            )
        )
        seen_urls.add(url)

    if not unique_items:
        raise MonitorError(
            "Could not find complete items in MFSA's official RSS feed. "
            "The feed format may have changed."
        )
    return unique_items


def parse_boe_pra_updates(
    html: str, base_url: str = BOE_PRA_URL
) -> List[UpdateSummary]:
    parser = BoePraUpdatesParser(base_url)
    parser.feed(html)
    parser.close()

    unique_items: List[UpdateSummary] = []
    seen_urls: Set[str] = set()
    for item in parser.items:
        if item.url not in seen_urls:
            unique_items.append(item)
            seen_urls.add(item.url)

    expected_sources = set(BoePraUpdatesParser.SOURCE_HEADINGS.values())
    found_sources = {item.source for item in unique_items}
    if not unique_items or found_sources != expected_sources:
        raise MonitorError(
            "Could not find complete 'Open consultations and discussion "
            "papers' and 'Latest policy' lists on the Bank of England PRA "
            "page. The page layout may have changed."
        )
    return unique_items


def _shorten_text(text: str, max_characters: int = 350) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_characters:
        return cleaned

    shortened = cleaned[: max_characters + 1]
    sentence_ends = [
        shortened.rfind(marker)
        for marker in (". ", "? ", "! ")
    ]
    sentence_end = max(sentence_ends)
    if sentence_end >= 80:
        return shortened[: sentence_end + 1].strip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,;:") + "..."


def _normalise_date(date_text: str) -> str:
    value = date_text.strip()
    formats = (
        ("%Y/%m/%d", value[:10]),
        ("%d/%m/%Y", value[:10]),
        ("%Y-%m-%d", value[:10]),
        ("%d %B %Y", value),
        ("%B %d, %Y", value),
        ("%A, %d %B %Y", value),
        ("%a, %d %b %Y %H:%M:%S %z", value),
    )
    for date_format, candidate in formats:
        try:
            return datetime.strptime(candidate, date_format).strftime("%d/%m/%Y")
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


def _is_recent(item: UpdateSummary) -> bool:
    return _date_value(item.issued_date).date() >= _recent_cutoff()


def _classify_update(title: str, description: str, source: str) -> str:
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
            ("consultation", "public consultation", "targeted consultation")
        )
        or re.match(r"^(?:cp|liac)\d", title_key)
        or "consultation paper" in title_key
        or "consultation" in source_key
    ):
        return "Consultation paper"
    if (
        "validation" in text
        or "data quality" in text
        or "assessment methodology" in text
    ):
        return "Validation rules / assessment"
    if (
        "reporting framework" in text
        or "reporting requirement" in text
        or "reporting template" in text
        or "regulatory reporting" in text
        or "regulatory return" in text
        or "disclosure" in text
        or re.search(r"\b(?:reporting|taxonomy|templates?)\b", title_key)
    ):
        return "Reporting framework"
    if source_key == "latest policy":
        if (
            "rule" in text
            or "standard" in text
            or "amendment" in text
        ):
            return "Regulation / standard-setting update"
        return "Regulatory or policy update"
    if source_key == "policy and guidance":
        if title_key.startswith("fg"):
            return "Supervisory expectations / guidance"
        if "rule" in text or "standard" in text or "amendment" in text:
            return "Regulation / standard-setting update"
        return "Regulatory or policy update"
    if (
        "supervisory" in text
        or "expectation" in text
        or "guidance" in text
        or "prudential" in text
        or "systemic risk" in text
        or "financial stability" in text
        or "operational resilience" in text
    ):
        return "Supervisory expectations / guidance"
    if (
        "rule" in text
        or "standard" in text
        or "amendment" in text
        or "regulatory framework" in text
    ):
        return "Regulation / standard-setting update"
    return "Regulatory or policy update"


def _item_is_relevant(item: UpdateSummary) -> bool:
    if item.authority == FCA_AUTHORITY:
        if item.source in {"Consultation", "Policy and guidance"}:
            return True
        return bool(REGULATORY_TERMS.search(item.title))
    if item.authority == BOE_PRA_AUTHORITY:
        return True
    if item.authority == MFSA_AUTHORITY:
        if re.search(
            r"\b(?:regulatory action|administrative penalty|"
            r"fit and proper|enforcement|sanction)\b",
            item.title,
            re.IGNORECASE,
        ):
            return False
        return bool(REGULATORY_TERMS.search(item.title))
    return bool(REGULATORY_TERMS.search(item.title))


def _deduplicate_items(items: List[UpdateSummary]) -> List[UpdateSummary]:
    unique: Dict[str, UpdateSummary] = {}
    for item in items:
        reference = ""
        if item.authority == MFSA_AUTHORITY:
            match = re.search(r"\bRef:\s*(\d{4}-\d+)\b", item.title, re.I)
            if match:
                reference = match.group(1)
        key = (
            f"{item.authority}|{item.issued_date}|ref:{reference}"
            if reference
            else f"{item.authority}|{item.url}"
        )
        existing = unique.get(key)
        if existing is None:
            unique[key] = item
        elif (
            item.title.casefold().startswith("regulatory action")
            and not existing.title.casefold().startswith("regulatory action")
        ):
            unique[key] = item
    return list(unique.values())


def _description_is_useful(title: str, description: str) -> bool:
    if len(description.strip()) < 50:
        return False
    if description.rstrip().endswith(("...", "…", "â€¦")):
        return False
    if description.casefold().startswith("reference number:"):
        return False
    normalised_title = re.sub(r"\s+", " ", title).strip().casefold()
    normalised_description = (
        re.sub(r"\s+", " ", description).strip().casefold()
    )
    return not (
        normalised_title == normalised_description
        or normalised_title.startswith(normalised_description)
        or (
            normalised_description.startswith(normalised_title)
            and len(normalised_description) <= len(normalised_title) + 20
        )
    )


def _human_date(date_text: str) -> str:
    parsed = _date_value(date_text)
    if parsed == datetime.min:
        return date_text
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def _consultation_status_sentence(deadline_date: str) -> str:
    if not deadline_date:
        return ""
    deadline = _date_value(deadline_date).date()
    if deadline < datetime.now(timezone.utc).date():
        return f"The consultation closed on {_human_date(deadline_date)}."
    return f"The consultation is open until {_human_date(deadline_date)}."


def _deadline_from_html(page_html: str) -> str:
    decoded = html_module.unescape(html_module.unescape(page_html))
    plain_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", decoded))
    patterns = (
        r"(?:consultation end date|expiry date|deadline|closing date|"
        r"open until|consultation closes|closes? on)\D{0,70}"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        r"(?:send\s+us\s+your\s+feedback|send\s+your\s+feedback)"
        r"\s+by\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        r"(?:responses?|comments?|feedback)\s+(?:are\s+)?(?:requested|"
        r"invited|should be submitted).*?\bby\s+(?:[A-Za-z]+,\s+)?"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        r"(?:consultation end date|consultation closes|expiry date|"
        r"deadline|closing date)"
        r"\D{0,70}(\d{1,2}/\d{1,2}/\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, plain_text, re.IGNORECASE)
        if match:
            normalised = _normalise_date(match.group(1))
            if normalised:
                return normalised
    return ""


def _fallback_description(item: UpdateSummary, update_type: str) -> str:
    authority = item.authority.split(" (", 1)[0]
    topic = re.sub(
        r"^(?:CP|LIAC)\d+/\d+\s*[–-]\s*",
        "",
        item.title,
        flags=re.IGNORECASE,
    ).strip(" .:-")
    if update_type == "Consultation paper":
        return f"{authority} is seeking stakeholder feedback on {topic}."
    if update_type == "Reporting framework":
        return (
            f"{authority} published a reporting framework update "
            f"concerning {topic}."
        )
    if update_type == "Supervisory expectations / guidance":
        return f"{authority} published supervisory guidance concerning {topic}."
    if update_type == "Regulation / standard-setting update":
        return f"{authority} issued a regulatory rules update concerning {topic}."
    return f"{authority} published a regulatory or policy update concerning {topic}."


def parse_detail_metadata(
    html: str, url: str, require_issued_date: bool
) -> DetailMetadata:
    parser = DetailMetadataParser()
    parser.feed(html)
    parser.close()
    description = parser.result()
    if not description:
        raise MonitorError(f"FCA did not provide a short description for {url}")

    issued_date = _normalise_date(parser.published_date)
    if require_issued_date and not issued_date:
        raise MonitorError(f"FCA did not provide a publication date for {url}")

    return DetailMetadata(description=description, issued_date=issued_date)


def parse_cbc_description(
    html: str,
    url: str,
    fetcher: Optional[Callable[[str, float], str]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    parser = CbcArticleParser(url)
    parser.feed(html)
    parser.close()
    if parser.description:
        return parser.description

    if parser.alternate_language_url and fetcher is not None:
        alternate_html = fetcher(parser.alternate_language_url, timeout)
        alternate_parser = CbcArticleParser(parser.alternate_language_url)
        alternate_parser.feed(alternate_html)
        alternate_parser.close()
        if alternate_parser.description:
            return alternate_parser.description

    raise MonitorError(
        "Central Bank of Cyprus did not provide an article description "
        f"for {url}"
    )


def parse_bog_description(html: str, url: str) -> str:
    parser = BogArticleParser()
    parser.feed(html)
    parser.close()
    if not parser.description:
        raise MonitorError(
            f"Bank of Greece did not provide an article description for {url}"
        )
    return parser.description


def parse_boe_pra_detail(html: str, url: str) -> DetailMetadata:
    parser = BoePraDetailParser()
    parser.feed(html)
    parser.close()

    excluded_prefixes = (
        "by responding to this consultation",
        "the response will be assessed",
        "the consultation paper will explain if responses",
        "information provided in response to this consultation",
        "please indicate if you regard",
        "when you respond to this consultation",
        "in the policy statement for this consultation",
        "please provide any comments",
        "when responding, please",
        "please also indicate",
        "for information on how the pra has addressed",
        "consultation end date:",
        "proposed implementation date:",
    )
    candidates: List[str] = []
    preferred: List[str] = []
    for paragraph in parser.paragraphs:
        is_numbered_section = bool(
            re.match(r"^\d+\.\d+\s+", paragraph)
        )
        cleaned = re.sub(r"^\d+\.\d+\s+", "", paragraph).strip()
        lowered = cleaned.lower()
        if len(cleaned) < 40 or lowered.startswith(excluded_prefixes):
            continue
        candidates.append(cleaned)
        if (
            (
                is_numbered_section
                and re.search(
                    r"\b(?:consultation paper|policy statement)\b",
                    lowered,
                )
            )
            or lowered.startswith("the pra proposes")
            or lowered.startswith("in liac")
            or (
                is_numbered_section
                and (
                    "the pra consulted" in lowered
                    or "provides feedback" in lowered
                    or "sets out the pra" in lowered
                )
            )
        ):
            preferred.append(cleaned)

    description = preferred[0] if preferred else (
        candidates[0] if candidates else ""
    )
    if description.endswith(":"):
        for candidate in candidates:
            if candidate != description and not candidate.endswith(":"):
                description = f"{description} {candidate}"
                break
    if not description:
        raise MonitorError(
            "Bank of England PRA did not provide a substantive description "
            f"for {url}"
        )

    issued_date = _normalise_date(parser.published_date)
    return DetailMetadata(
        description=_shorten_text(description),
        issued_date=issued_date,
    )


def parse_generic_description(
    html: str, title: str, url: str
) -> str:
    parser = GenericDescriptionParser(title)
    parser.feed(html)
    parser.close()
    description = parser.result()
    if not description:
        raise MonitorError(
            f"The official page did not provide a usable description for {url}"
        )
    return description


def _complete_description(
    item: UpdateSummary,
    detail_html: str,
    update_type: str,
    fetcher: Callable[[str, float], str],
    timeout: float,
) -> tuple[str, str]:
    issued_date = item.issued_date
    try:
        if item.authority == CBC_AUTHORITY:
            description = parse_cbc_description(
                detail_html,
                item.url,
                fetcher=fetcher,
                timeout=timeout,
            )
        elif item.authority == BOG_AUTHORITY:
            description = parse_bog_description(detail_html, item.url)
        elif item.authority == BOE_PRA_AUTHORITY:
            detail = parse_boe_pra_detail(detail_html, item.url)
            description = detail.description
            issued_date = issued_date or detail.issued_date
        elif item.authority == FCA_AUTHORITY:
            detail = parse_detail_metadata(
                detail_html,
                item.url,
                require_issued_date=not bool(issued_date),
            )
            description = detail.description
            issued_date = issued_date or detail.issued_date
        else:
            description = parse_generic_description(
                detail_html, item.title, item.url
            )
    except MonitorError:
        description = item.description

    if not _description_is_useful(item.title, description):
        description = _fallback_description(item, update_type)

    if update_type == "Consultation paper":
        description = _shorten_text(description)
        deadline = _deadline_from_html(detail_html)
        status = _consultation_status_sentence(deadline)
        if not status:
            status = (
                "The official page did not expose a response deadline "
                "to the monitor."
            )
        if status not in description:
            description = f"{description.rstrip()} {status}"
        return description, issued_date
    return _shorten_text(description), issued_date


def _empty_state() -> Dict[str, Any]:
    return {"version": 6, "last_checked_utc": None, "updates": []}


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _empty_state()

    try:
        with path.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(
            f"Could not read saved state at {path}. "
            "Move or repair that file before running the monitor again."
        ) from exc

    if not isinstance(state, dict) or not isinstance(state.get("updates"), list):
        raise MonitorError(f"Saved state has an invalid format: {path}")

    for record in state["updates"]:
        if not isinstance(record, dict) or not isinstance(record.get("url"), str):
            raise MonitorError(f"Saved state contains an invalid update: {path}")

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
        raise MonitorError(f"Could not save update history to {path}: {exc}") from exc


def check_for_updates(
    state_path: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    fetcher: Optional[Callable[[str, float], str]] = None,
) -> List[RegulatoryUpdate]:
    if timeout <= 0:
        raise MonitorError("Timeout must be greater than zero.")

    get_page = fetcher or fetch_text
    current_items: List[UpdateSummary] = []
    warnings: List[str] = []

    listing_sources = (
        ("FCA latest news", SOURCE_URL, parse_latest_updates),
        (
            "FCA publications",
            PUBLICATIONS_URL,
            parse_latest_publications,
        ),
        (
            "Central Bank of Cyprus announcements",
            CBC_HOME_URL,
            parse_cbc_announcements,
        ),
        ("Bank of Greece news", BOG_HOME_URL, parse_bog_homepage),
        (
            "Bank of England PRA updates",
            BOE_PRA_URL,
            parse_boe_pra_updates,
        ),
    )
    for source_name, url, parser in listing_sources:
        try:
            current_items.extend(parser(get_page(url, timeout)))
        except MonitorError as exc:
            warnings.append(f"{source_name} could not be checked: {exc}")

    try:
        if fetcher is None:
            mfsa_feed_xml = fetch_text(
                MFSA_FEED_URL,
                timeout,
                accepted_content_types={
                    "application/rss+xml",
                    "application/xml",
                    "text/xml",
                },
            )
        else:
            mfsa_feed_xml = get_page(MFSA_FEED_URL, timeout)
        current_items.extend(parse_mfsa_feed(mfsa_feed_xml))
    except MonitorError as exc:
        warnings.append(f"MFSA updates could not be checked: {exc}")

    if not current_items:
        raise MonitorError(
            "No Group 3 source could be checked successfully. "
            + " ".join(warnings)
        )
    current_items = [
        item
        for item in _deduplicate_items(current_items)
        if (not item.issued_date or _is_recent(item))
        and _item_is_relevant(item)
    ]
    state = load_state(state_path)
    seen_urls = {
        record["url"]
        for record in state["updates"]
        if isinstance(record, dict) and isinstance(record.get("url"), str)
    }

    new_updates: List[RegulatoryUpdate] = []
    for item in current_items:
        if item.url in seen_urls:
            continue

        update_type = _classify_update(
            item.title, item.description, item.source
        )
        description = item.description
        issued_date = item.issued_date
        mfsa_feed_deadline = (
            _deadline_from_html(item.description)
            if item.authority == MFSA_AUTHORITY
            and update_type == "Consultation paper"
            else ""
        )
        if mfsa_feed_deadline:
            description = (
                f"{_fallback_description(item, update_type)} "
                f"{_consultation_status_sentence(mfsa_feed_deadline)}"
            )
        needs_detail = (
            not mfsa_feed_deadline
            and (
                update_type == "Consultation paper"
                or item.authority
                in {
                    CBC_AUTHORITY,
                    BOG_AUTHORITY,
                    MFSA_AUTHORITY,
                    BOE_PRA_AUTHORITY,
                }
                or not _description_is_useful(item.title, description)
            )
        )
        if needs_detail:
            try:
                detail_html = get_page(item.url, timeout)
                description, issued_date = _complete_description(
                    item,
                    detail_html,
                    update_type,
                    get_page,
                    timeout,
                )
            except MonitorError as exc:
                warnings.append(
                    f"Detail page could not be checked for "
                    f"{item.title}: {exc}"
                )
                deadline_source = description
                if not _description_is_useful(item.title, description):
                    description = _fallback_description(item, update_type)
                if update_type == "Consultation paper":
                    deadline = _deadline_from_html(deadline_source)
                    status = _consultation_status_sentence(deadline)
                    if not status:
                        status = (
                            "The official page did not expose a response "
                            "deadline to the monitor."
                        )
                    description = f"{_shorten_text(description).rstrip()} {status}"

        if not issued_date:
            warnings.append(
                f"Skipped {item.title} because its official page did not "
                "provide a publication date."
            )
            continue
        if _date_value(issued_date).date() < _recent_cutoff():
            continue
        if not _description_is_useful(item.title, description):
            description = _fallback_description(item, update_type)

        if not description:
            warnings.append(
                f"Skipped {item.title} because no useful description "
                "could be produced."
            )
            continue

        new_updates.append(
            RegulatoryUpdate(
                title=item.title,
                description=description,
                issued_date=issued_date,
                url=item.url,
                authority=item.authority,
                source=item.source,
                update_type=_classify_update(
                    item.title, description, item.source
                ),
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

    state["version"] = 6
    state["last_checked_utc"] = datetime.now(timezone.utc).isoformat()
    state["updates"].extend(asdict(update) for update in new_updates)
    save_state(state_path, state)
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    return new_updates


def print_updates(updates: List[RegulatoryUpdate]) -> None:
    updates_by_authority = {
        authority: [
            update for update in updates if update.authority == authority
        ]
        for authority in AUTHORITY_ORDER
    }

    for authority_index, authority in enumerate(AUTHORITY_ORDER):
        authority_updates = updates_by_authority[authority]
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
            "Print unseen regulatory updates grouped by authority. Sources "
            "currently include the FCA's latest news and publications lists, "
            "Central Bank of Cyprus homepage announcements, and Bank of Greece "
            "homepage news, the MFSA official feed, and Bank of England PRA "
            "consultations and policy."
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
    """Ensure Windows terminals can display Greek and other Unicode text."""
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
