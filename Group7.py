#!/usr/bin/env python3
"""Print unseen regulatory updates from the European Banking Federation."""

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
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


EBF_HOME_URL = "https://www.ebf.eu/"
EBF_API_URL = "https://www.ebf.eu/wp-json/wp/v2/posts"
EBF_AUTHORITY = "European Banking Federation (EBF)"
EBF_SOURCE = "EBF official publications and positions"

DEFAULT_STATE_FILE = Path(__file__).with_name("group7_updates_state.json")
DEFAULT_TIMEOUT_SECONDS = 45.0
RECENT_LOOKBACK_DAYS = 180
MAX_RESPONSE_BYTES = 6_000_000
MAX_POSTS = 100
USER_AGENT = (
    "Group7-Regulatory-Updates-Monitor/1.0 "
    "(personal checker for official regulatory websites)"
)

REGULATORY_TERMS = re.compile(
    r"\b(?:"
    r"regulat(?:ion|ory)|supervis(?:ion|ory)|legislat(?:ion|ive)|"
    r"directive|rules?|standards?|guidelines?|consultation|"
    r"call for evidence|technical standards?|"
    r"reporting|disclosures?|taxonomy|data framework|"
    r"prudential|capital treatment|capital requirements?|"
    r"systemic risk buffer|stress testing|SREP|shadow banking|"
    r"clearing|digital euro|"
    r"financial sector simplification|banking package|"
    r"market integration|sustainability reporting|"
    r"tax framework|financial regulation"
    r")\b",
    re.IGNORECASE,
)

REGULATORY_ACRONYMS = re.compile(
    r"\b(?:RTS|ITS|AMLA|AMLR|AML|CFT|EBA|ESMA|ECB|IReF|"
    r"MiFIR|MiCA|SREP|ESRS|DNSH)\b"
)

EXCLUDED_TERMS = re.compile(
    r"\b(?:"
    r"vacanc(?:y|ies)|trainee|job opening|"
    r"money quiz|financial education|"
    r"welcome progress to strengthen European retail payments|"
    r"elects? new leadership|chair and two vice-chairs|"
    r"board highlights|event|conference|webinar|podcast|"
    r"psychosocial risks in the workplace|"
    r"transportation sector|sustainable transport investment plan"
    r")\b",
    re.IGNORECASE,
)


class MonitorError(RuntimeError):
    """An expected problem that can be explained cleanly to the user."""


@dataclass(frozen=True)
class UpdateSummary:
    title: str
    description: str
    issued_date: str
    url: str
    source: str
    update_type: str

    @property
    def identifier(self) -> str:
        identity = "\n".join(
            (
                EBF_AUTHORITY.casefold(),
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


def _clean_text(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        html_module.unescape(value),
    ).strip()


def _strip_markup(value: str) -> str:
    return _clean_text(re.sub(r"<[^>]+>", " ", value))


def _normalise_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or hostname not in {"ebf.eu", "www.ebf.eu"}
    ):
        return ""
    path = re.sub(r"/{2,}", "/", parsed.path)
    if path != "/":
        path = path.rstrip("/")
    return urlunparse(
        parsed._replace(
            scheme="https",
            netloc="www.ebf.eu",
            path=path,
            query="",
            fragment="",
        )
    )


def _normalise_date(value: str) -> str:
    candidate = value.strip()
    for date_format, text in (
        ("%Y-%m-%d", candidate[:10]),
        ("%Y-%m-%dT%H:%M:%S", candidate[:19]),
        ("%d/%m/%Y", candidate[:10]),
        ("%d %B %Y", candidate),
        ("%d %b %Y", candidate),
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


def _complete_short_description(
    text: str, max_characters: int = 560
) -> str:
    cleaned = _clean_text(text)
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
        if length >= 160:
            break
    if selected:
        return " ".join(selected)
    return cleaned


class ContentParagraphParser(HTMLParser):
    """Extract meaningful paragraphs while ignoring embedded CSS and scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: List[str] = []
        self._ignored_depth = 0
        self._capture_depth = 0
        self._parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"p", "blockquote"}:
            if self._capture_depth:
                self._capture_depth += 1
            else:
                self._capture_depth = 1
                self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_depth and not self._ignored_depth:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if tag in {"p", "blockquote"} and self._capture_depth:
            self._capture_depth -= 1
            if self._capture_depth == 0:
                value = _clean_text("".join(self._parts))
                if value:
                    self.paragraphs.append(value)


def _plain_content_text(rendered_html: str) -> str:
    parser = ContentParagraphParser()
    parser.feed(rendered_html)
    parser.close()
    return _clean_text(" ".join(parser.paragraphs))


def _paragraph_is_useful(title: str, paragraph: str) -> bool:
    text = paragraph.strip()
    key = text.casefold()
    title_key = title.casefold()
    if len(text) < 70:
        return False
    if key == title_key or title_key in key and len(key) < len(title_key) + 40:
        return False
    if key.startswith(
        (
            "for more information",
            "for further information",
            "about the ebf",
            "the european banking federation is the voice",
            "the ebf produces a daily",
            "click here to subscribe",
            "sorry. no data",
        )
    ):
        return False
    if (
        "background-image:" in key
        or "#top ." in key
        or "border-radius:" in key
    ):
        return False
    return True


def _description_from_content(title: str, rendered_html: str) -> str:
    parser = ContentParagraphParser()
    parser.feed(rendered_html)
    parser.close()

    dated_action = re.compile(
        r"\bBrussels,\s+(?:(?:\d{1,2}\s+)?[A-Za-z]+\s+\d{4})"
        r"\s*[–—-]\s*",
        re.IGNORECASE,
    )

    def clean_paragraph(paragraph: str) -> str:
        match = dated_action.search(paragraph)
        if match:
            paragraph = paragraph[match.end():]
        return _clean_text(paragraph)

    candidates = [
        clean_paragraph(paragraph)
        for paragraph in parser.paragraphs
        if _paragraph_is_useful(title, clean_paragraph(paragraph))
    ]
    if not candidates:
        return ""

    def information_score(paragraph: str) -> int:
        score = 0
        if re.search(
            r"\b(?:key conclusions?|calls?|urges?|recommends?|"
            r"supports?|concerned|believes?|warns?|emphasises?)\b",
            paragraph,
            re.IGNORECASE,
        ):
            score += 7
        if re.search(
            r"\b(?:responded|submitted|published|commissioned|"
            r"welcomes?|presented|outlines?|addresses?)\b",
            paragraph,
            re.IGNORECASE,
        ):
            score += 5
        if REGULATORY_TERMS.search(paragraph) or REGULATORY_ACRONYMS.search(
            paragraph
        ):
            score += 3
        if 110 <= len(paragraph) <= 650:
            score += 2
        if paragraph.rstrip().endswith(":"):
            score -= 6
        if re.match(r"^(?:What|Why|How|When)\b", paragraph):
            score -= 4
        return score

    description = max(
        enumerate(candidates),
        key=lambda candidate: (
            information_score(candidate[1]),
            -candidate[0],
        )
    )[1]
    description = re.sub(
        r"\bEuropean Banking Federation\(EBF\)",
        "European Banking Federation (EBF)",
        description,
    )
    description = re.sub(
        r"\bconcerned abouts its\b",
        "concerned about its",
        description,
        flags=re.IGNORECASE,
    )
    low_information = (
        re.match(r"^In this document\b", description, re.IGNORECASE)
        or re.search(
            r"\b(?:the note is available below|read the full EBF note)\b",
            description,
            re.IGNORECASE,
        )
        or (
            "key messages on" in description.casefold()
            and not re.search(
                r"\b(?:supports?|calls?|urges?|recommends?|"
                r"concerned|believes?|warns?|responded|submitted)\b",
                description,
                re.IGNORECASE,
            )
        )
        or (
            re.search(
                r"\b(?:call(?:s|ed|ing)?|urg(?:e|es|ed|ing)|"
                r"joins? (?:a )?call)\b",
                title,
                re.IGNORECASE,
            )
            and not re.search(
                r"\b(?:call(?:s|ed|ing)?|urg(?:e|es|ed|ing)|"
                r"asks?|requests?|seeks?)\b",
                description,
                re.IGNORECASE,
            )
        )
    )
    if low_information:
        return ""
    description = re.sub(
        r"\s+(?:The|These) (?:key messages|report['’]s key insights)"
        r".*?:\s*$",
        "",
        description,
        flags=re.IGNORECASE,
    ).strip()
    if description and description[-1] not in ".!?":
        description += "."
    return _complete_short_description(description)


def _is_regulatory_item(title: str, description: str) -> bool:
    combined = f"{title} {description}"
    if EXCLUDED_TERMS.search(combined):
        return False
    return bool(
        REGULATORY_TERMS.search(combined)
        or REGULATORY_ACRONYMS.search(combined)
    )


def _is_consultation_response(title: str, description: str) -> bool:
    text = f"{title} {description}".casefold()
    response_signal = re.search(
        r"\b(?:response|responds?|feedback|comments?|"
        r"call for evidence|position paper|preliminary views)\b",
        text,
    )
    consultation_signal = re.search(
        r"\b(?:consultation|draft guidelines?|"
        r"draft (?:regulatory|implementing) technical standards?|"
        r"call for evidence|proposed|proposal)\b",
        text,
        re.IGNORECASE,
    )
    acronym_consultation_signal = re.search(
        r"\bdraft (?:RTS|ITS)\b",
        f"{title} {description}",
    )
    return bool(
        response_signal
        and (consultation_signal or acronym_consultation_signal)
    )


def _classify_update(
    title: str, description: str, source: str
) -> str:
    text = f"{title} {description} {source}"
    key = text.casefold()
    title_key = title.casefold()

    if _is_consultation_response(title, description):
        return "Industry consultation response"
    if (
        title_key.startswith(("public consultation", "ebf consultation"))
        and "response" not in title_key
    ):
        return "Consultation paper"
    if re.search(
        r"\b(?:reporting framework|IReF|reporting standards?|"
        r"disclosures?|taxonomy|data framework)\b",
        text,
        re.IGNORECASE,
    ):
        return "Reporting framework"
    if re.search(
        r"\b(?:validation|data quality|implementation assessment|"
        r"alignment assessment|stress testing|risk assessment)\b",
        text,
        re.IGNORECASE,
    ):
        return "Validation rules / assessment"
    if re.search(
        r"\b(?:(?:position|white|policy) paper|"
        r"publish(?:ed|es) (?:a )?paper|paper on|"
        r"preliminary views|joint letter|(?:joint )?statement|"
        r"note on|calls? for|urges?)\b",
        text,
        re.IGNORECASE,
    ):
        return "Industry position / policy paper"
    if re.search(
        r"\b(?:draft )?(?:regulatory technical standard|"
        r"implementing technical standard|regulation|directive|"
        r"rules?|standards?)\b",
        text,
        re.IGNORECASE,
    ) or re.search(r"\b(?:RTS|ITS)\b", text):
        return "Regulation / standard-setting update"
    if re.search(
        r"\b(?:supervisory|guidelines?|SREP|prudential|"
        r"systemic risk buffer|capital treatment)\b",
        text,
        re.IGNORECASE,
    ):
        return "Supervisory expectations / guidance"
    if re.search(r"\b(?:study|research report|cost study)\b", key):
        return "Industry research / policy report"
    return "Regulatory or policy update"


def _human_date(date_text: str) -> str:
    parsed = _date_value(date_text)
    if parsed == datetime.min:
        return date_text
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def _deadline_from_html(rendered_html: str) -> str:
    plain_text = _strip_markup(rendered_html)
    patterns = (
        r"(?:deadline|closing date|consultation closes|open until)"
        r"\D{0,70}(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        r"(?:responses?|comments?|feedback)"
        r"(?:\s+on\s+the\s+consultation)?\s+"
        r"(?:should|must)?\s*(?:be\s+)?submitted.*?\bby\s+"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, plain_text, re.IGNORECASE)
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


def _topic_after_prefix(title: str) -> str:
    topic = re.sub(
        r"^(?:EBF\s+)?(?:response|responds?|feedback|position paper|"
        r"preliminary views|joint letter|statement|note|"
        r"joins? (?:a )?call)\s*"
        r"(?:to|on|for|:)?\s*",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip(" .:-")
    return topic or title


def _fallback_description(title: str, update_type: str) -> str:
    topic = _topic_after_prefix(title)
    if update_type == "Industry consultation response":
        return (
            "The European Banking Federation published the banking "
            f"industry's response concerning {topic}."
        )
    if update_type == "Industry position / policy paper":
        if re.search(
            r"\bjoins? (?:a )?call\b",
            title,
            re.IGNORECASE,
        ):
            return (
                "The European Banking Federation joined an industry call "
                f"for {topic}."
            )
        return (
            "The European Banking Federation published an industry "
            f"position concerning {topic}."
        )
    if update_type == "Reporting framework":
        return (
            "The European Banking Federation published an update concerning "
            f"the reporting framework for {topic}."
        )
    if update_type == "Industry research / policy report":
        return (
            "The European Banking Federation published research concerning "
            f"{topic}."
        )
    return (
        "The European Banking Federation published a regulatory policy "
        f"update concerning {topic}."
    )


def _build_feed_url() -> str:
    after = _recent_cutoff().strftime("%Y-%m-%dT00:00:00")
    query = urlencode(
        {
            "per_page": str(MAX_POSTS),
            "after": after,
            "orderby": "date",
            "order": "desc",
            "_fields": (
                "id,date,link,title,content,categories,tags"
            ),
        }
    )
    return f"{EBF_API_URL}?{query}"


def fetch_json(url: str, timeout: float) -> Any:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,*/*;q=0.1",
            "Accept-Language": "en-GB,en;q=0.8",
        },
    )
    last_error: Optional[BaseException] = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get_content_type()
                if content_type not in {
                    "application/json",
                    "text/json",
                    "text/plain",
                }:
                    raise MonitorError(
                        f"EBF returned unexpected content type "
                        f"{content_type!r}."
                    )
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise MonitorError(
                        "The EBF publication feed was unexpectedly large."
                    )
                charset = response.headers.get_content_charset() or "utf-8"
                try:
                    return json.loads(
                        content.decode(charset, errors="replace")
                    )
                except json.JSONDecodeError as exc:
                    raise MonitorError(
                        "EBF returned invalid publication data."
                    ) from exc
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt:
                raise MonitorError(
                    f"EBF returned HTTP {exc.code} while requesting its "
                    "publication feed."
                ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt:
                reason = getattr(exc, "reason", exc)
                raise MonitorError(
                    f"Could not retrieve the EBF publication feed: {reason}"
                ) from exc
        time.sleep(1.0)
    raise MonitorError(
        f"Could not retrieve the EBF publication feed: {last_error}"
    )


def parse_ebf_feed(data: Any) -> List[UpdateSummary]:
    if not isinstance(data, list):
        raise MonitorError("EBF returned an invalid publication feed.")

    items: List[UpdateSummary] = []
    for record in data:
        if not isinstance(record, dict):
            continue
        title_data = record.get("title")
        content_data = record.get("content")
        title_html = (
            title_data.get("rendered", "")
            if isinstance(title_data, dict)
            else ""
        )
        content_html = (
            content_data.get("rendered", "")
            if isinstance(content_data, dict)
            else ""
        )
        title = _strip_markup(str(title_html))
        issued_date = _normalise_date(str(record.get("date") or ""))
        url = _normalise_url(str(record.get("link") or ""))
        description = _description_from_content(title, str(content_html))
        content_text = _plain_content_text(str(content_html))
        if (
            not title
            or not issued_date
            or not url
            or not _is_recent(issued_date)
            or not _is_regulatory_item(title, content_text)
        ):
            continue

        update_type = _classify_update(
            title,
            f"{description} {content_text}",
            EBF_SOURCE,
        )
        if not description:
            description = _fallback_description(title, update_type)
        if update_type == "Consultation paper":
            status = _consultation_status_sentence(
                _deadline_from_html(str(content_html))
            )
            description = f"{description.rstrip()} {status}"

        items.append(
            UpdateSummary(
                title=title,
                description=_complete_short_description(description),
                issued_date=issued_date,
                url=url,
                source=EBF_SOURCE,
                update_type=update_type,
            )
        )

    if not items:
        raise MonitorError(
            "No recent regulatory EBF publications were found. "
            "The EBF feed structure may have changed."
        )
    return _unique_items(items)


def _unique_items(
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
            f"Could not read saved Group 7 history {path}: {exc}"
        ) from exc
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(state.get("updates"), list)
    ):
        raise MonitorError(f"Saved Group 7 history is invalid: {path}")
    for record in state["updates"]:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("identifier"), str)
        ):
            raise MonitorError(
                f"Saved Group 7 history contains an invalid item: {path}"
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
            f"Could not save Group 7 history to {path}: {exc}"
        ) from exc


def check_for_updates(
    state_path: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    json_fetcher: Optional[Callable[[str, float], Any]] = None,
) -> List[RegulatoryUpdate]:
    if timeout <= 0:
        raise MonitorError("Timeout must be greater than zero.")
    get_json = json_fetcher or fetch_json
    current_items = parse_ebf_feed(
        get_json(_build_feed_url(), timeout)
    )
    state = load_state(state_path)
    seen_identifiers = {
        record["identifier"]
        for record in state["updates"]
        if isinstance(record, dict)
        and isinstance(record.get("identifier"), str)
    }

    new_updates = [
        RegulatoryUpdate(
            identifier=item.identifier,
            title=item.title,
            description=item.description,
            issued_date=item.issued_date,
            url=item.url,
            authority=EBF_AUTHORITY,
            source=item.source,
            update_type=item.update_type,
        )
        for item in current_items
        if item.identifier not in seen_identifiers
    ]
    new_updates.sort(
        key=lambda update: (
            -_date_value(update.issued_date).toordinal(),
            update.title.casefold(),
        )
    )

    state["last_checked_utc"] = datetime.now(timezone.utc).isoformat()
    state["updates"].extend(asdict(update) for update in new_updates)
    save_state(state_path, state)
    return new_updates


def print_updates(updates: List[RegulatoryUpdate]) -> None:
    print(EBF_AUTHORITY)
    print("=" * len(EBF_AUTHORITY))
    if not updates:
        print("No new updates available")
        return

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


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Print unseen recent regulatory publications from the "
            "European Banking Federation."
        )
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=(
            "where to save Group 7 update history "
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
