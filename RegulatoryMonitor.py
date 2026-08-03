#!/usr/bin/env python3
"""Run all seven regulatory-update groups and print one combined report."""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import Group1
import Group2
import Group3
import Group7
import group4
import group5
import group6
from email_delivery import (
    EmailConfigurationError,
    EmailDeliveryError,
    EmailSettings,
    load_email_settings,
    send_report_via_outlook,
)


DEFAULT_TIMEOUT_SECONDS = 45.0
UPDATE_COUNT_PATTERN = re.compile(r"(?m)^(\d+)\s+new update\(s\)\s*$")


@dataclass(frozen=True)
class GroupSpec:
    number: int
    display_name: str
    state_filename: str
    runner: Callable[[Optional[List[str]]], int]
    sources: str


@dataclass(frozen=True)
class GroupResult:
    number: int
    display_name: str
    state_file: Path
    status: str
    exit_code: int
    update_count: int
    warning_count: int
    duration_seconds: float
    output: str
    messages: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


GROUPS: Tuple[GroupSpec, ...] = (
    GroupSpec(
        number=1,
        display_name="Group 1",
        state_filename="group1_updates_state.json",
        runner=Group1.main,
        sources="EBA, ECB Banking Supervision / SSM, DG FISMA and EUR-Lex",
    ),
    GroupSpec(
        number=2,
        display_name="Group 2",
        state_filename="group2_updates_state.json",
        runner=Group2.main,
        sources="EIOPA and IAIS",
    ),
    GroupSpec(
        number=3,
        display_name="Group 3",
        state_filename="fca_updates_state.json",
        runner=Group3.main,
        sources=(
            "FCA, Central Bank of Cyprus, Bank of Greece, MFSA and "
            "Bank of England PRA"
        ),
    ),
    GroupSpec(
        number=4,
        display_name="Group 4",
        state_filename="group4_updates_state.json",
        runner=group4.main,
        sources="SRB, DG FISMA and FSB",
    ),
    GroupSpec(
        number=5,
        display_name="Group 5",
        state_filename="group5_updates_state.json",
        runner=group5.main,
        sources="BCBS, BIS committees, FSB, FATF and IFRS Foundation / IASB",
    ),
    GroupSpec(
        number=6,
        display_name="Group 6",
        state_filename="group6_updates_state.json",
        runner=group6.main,
        sources="ESRB, AMLA, FATF, FSB, ESMA, DG FISMA and EUR-Lex",
    ),
    GroupSpec(
        number=7,
        display_name="Group 7",
        state_filename="group7_updates_state.json",
        runner=Group7.main,
        sources="European Banking Federation (EBF)",
    ),
)
GROUP_BY_NUMBER: Dict[int, GroupSpec] = {
    group.number: group for group in GROUPS
}


def _application_directory() -> Path:
    """Return the persistent folder beside the script or packaged executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_state_directory() -> Path:
    """Use legacy state files while developing and a state folder when frozen."""
    application_directory = _application_directory()
    if getattr(sys, "frozen", False):
        return application_directory / "state"
    return application_directory


def default_email_config_path() -> Path:
    return _application_directory() / "email_settings.json"


def default_reports_directory() -> Path:
    return _application_directory() / "reports"


def default_email_log_path() -> Path:
    return _application_directory() / "logs" / "email_delivery.log"


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _parse_groups(value: str) -> Tuple[int, ...]:
    text = value.strip().casefold()
    if text in {"all", "*"}:
        return tuple(group.number for group in GROUPS)
    if not text:
        raise argparse.ArgumentTypeError(
            "select at least one group, for example --groups 1,2,3"
        )

    numbers: List[int] = []
    for part in text.split(","):
        candidate = part.strip()
        if not candidate.isdigit():
            raise argparse.ArgumentTypeError(
                "groups must be 'all' or comma-separated numbers from 1 to 7"
            )
        number = int(candidate)
        if number not in GROUP_BY_NUMBER:
            raise argparse.ArgumentTypeError(
                f"group {number} is invalid; choose numbers from 1 to 7"
            )
        if number not in numbers:
            numbers.append(number)
    return tuple(numbers)


def _count_warnings(output: str, messages: str) -> int:
    combined = "\n".join((output, messages))
    return sum(
        1
        for line in combined.splitlines()
        if line.strip().casefold().startswith("warning:")
    )


def _count_updates(output: str) -> int:
    return sum(
        int(match.group(1))
        for match in UPDATE_COUNT_PATTERN.finditer(output)
    )


def run_group(
    group: GroupSpec,
    state_directory: Path,
    timeout: float,
) -> GroupResult:
    """Run one group while capturing its terminal output."""
    state_file = state_directory / group.state_filename
    standard_output = io.StringIO()
    error_output = io.StringIO()
    started = time.monotonic()
    exit_code = 1

    try:
        with redirect_stdout(standard_output), redirect_stderr(error_output):
            exit_code = int(
                group.runner(
                    [
                        "--state-file",
                        str(state_file),
                        "--timeout",
                        str(timeout),
                    ]
                )
            )
    except SystemExit as exc:
        if isinstance(exc.code, int):
            exit_code = exc.code
        else:
            exit_code = 1
        print(
            f"Group runner stopped unexpectedly: {exc}",
            file=error_output,
        )
    except Exception as exc:
        exit_code = 1
        print(
            f"Unexpected {type(exc).__name__}: {exc}",
            file=error_output,
        )

    duration = time.monotonic() - started
    output = standard_output.getvalue().strip()
    messages = error_output.getvalue().strip()
    status = "Completed" if exit_code == 0 else "Failed"
    return GroupResult(
        number=group.number,
        display_name=group.display_name,
        state_file=state_file,
        status=status,
        exit_code=exit_code,
        update_count=_count_updates(output),
        warning_count=_count_warnings(output, messages),
        duration_seconds=duration,
        output=output,
        messages=messages,
    )


def run_selected_groups(
    group_numbers: Sequence[int],
    state_directory: Path,
    timeout: float,
) -> List[GroupResult]:
    """Run requested groups in numerical order and continue after failures."""
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    state_directory.mkdir(parents=True, exist_ok=True)
    results: List[GroupResult] = []
    for number in sorted(group_numbers):
        results.append(
            run_group(
                GROUP_BY_NUMBER[number],
                state_directory=state_directory,
                timeout=timeout,
            )
        )
    return results


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds:.1f}s"
    return f"{seconds:.1f}s"


def build_combined_report(
    results: Sequence[GroupResult],
    started_at: datetime,
    finished_at: datetime,
) -> str:
    """Create the complete text report used by the console and future email."""
    total_updates = sum(result.update_count for result in results)
    failed = [result for result in results if not result.succeeded]
    warning_total = sum(result.warning_count for result in results)
    elapsed = (finished_at - started_at).total_seconds()

    lines: List[str] = [
        "REGULATORY UPDATES MONITOR",
        "=" * 26,
        f"Started: {started_at.astimezone().strftime('%d/%m/%Y %H:%M:%S %Z')}",
        f"Finished: {finished_at.astimezone().strftime('%d/%m/%Y %H:%M:%S %Z')}",
        f"Duration: {_format_duration(elapsed)}",
        "",
        "OVERALL SUMMARY",
        "---------------",
        f"Groups checked: {len(results)}",
        f"New updates found: {total_updates}",
        f"Warnings: {warning_total}",
        f"Failed groups: {len(failed)}",
        "",
    ]

    for result in results:
        summary = (
            f"{result.display_name}: {result.status} | "
            f"{result.update_count} new update(s) | "
            f"{result.warning_count} warning(s) | "
            f"{_format_duration(result.duration_seconds)}"
        )
        lines.append(summary)

    lines.extend(("", "DETAILED RESULTS", "=" * 16))
    for result in results:
        heading = f"{result.display_name} — {result.status}"
        lines.extend(("", heading, "-" * len(heading)))
        if result.output:
            lines.append(result.output)
        elif result.succeeded:
            lines.append("No output was produced.")
        else:
            lines.append("The group did not produce a results report.")
        if result.messages:
            lines.extend(("", "Messages:", result.messages))

    lines.extend(("", "FINAL STATUS", "------------"))
    if failed:
        failed_names = ", ".join(result.display_name for result in failed)
        lines.append(
            "Completed with failures. The other groups were still checked. "
            f"Failed: {failed_names}."
        )
    elif total_updates:
        lines.append(
            f"Completed successfully with {total_updates} new update(s)."
        )
    else:
        lines.append(
            "Completed successfully. No new updates are available."
        )
    return "\n".join(lines).rstrip() + "\n"


def save_report(path: Path, report: str) -> None:
    """Save a report atomically so a partial file is never left behind."""
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
            temporary_file.write(report)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise RuntimeError(f"Could not save report to {path}: {exc}") from exc


def timestamped_report_path(
    reports_directory: Path,
    finished_at: datetime,
) -> Path:
    """Return a non-conflicting local filename for an emailed report."""
    local_time = finished_at.astimezone()
    base_name = local_time.strftime("Regulatory_Report_%Y-%m-%d_%H%M%S")
    candidate = reports_directory / f"{base_name}.txt"
    suffix = 1
    while candidate.exists():
        candidate = reports_directory / f"{base_name}_{suffix:02d}.txt"
        suffix += 1
    return candidate


def build_email_subject(
    settings: EmailSettings,
    results: Sequence[GroupResult],
    finished_at: datetime,
) -> str:
    total_updates = sum(result.update_count for result in results)
    failed_count = sum(1 for result in results if not result.succeeded)
    status = "PARTIAL - " if failed_count else ""
    date_text = finished_at.astimezone().strftime("%d/%m/%Y")
    return (
        f"{settings.subject_prefix} - {date_text} - "
        f"{status}{total_updates} new update(s)"
    )


def append_email_log(path: Path, status: str, details: str) -> None:
    """Append a small delivery audit entry without recording recipients."""
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )
    clean_details = " ".join(details.splitlines()).strip()
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} | {status} | {clean_details}\n")


def _copy_live_states_to_staging(
    group_numbers: Sequence[int],
    live_directory: Path,
    staging_directory: Path,
) -> None:
    staging_directory.mkdir(parents=True, exist_ok=True)
    for number in group_numbers:
        filename = GROUP_BY_NUMBER[number].state_filename
        live_file = live_directory / filename
        if live_file.is_file():
            shutil.copy2(live_file, staging_directory / filename)


def _validate_staged_states(
    results: Sequence[GroupResult],
    staging_directory: Path,
) -> None:
    """Ensure successful groups produced state before an email is sent."""
    for result in results:
        if not result.succeeded:
            continue
        candidate = staging_directory / result.state_file.name
        if not candidate.is_file() or candidate.stat().st_size == 0:
            raise RuntimeError(
                f"{result.display_name} did not produce a valid staged state "
                "file, so the report was not emailed."
            )


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary_name = ""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as source_file, tempfile.NamedTemporaryFile(
            "wb",
            dir=str(destination.parent),
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            shutil.copyfileobj(source_file, temporary_file)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, destination)
    except OSError as exc:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise RuntimeError(
            f"Could not update state file {destination}: {exc}"
        ) from exc


def commit_emailed_states(
    results: Sequence[GroupResult],
    live_directory: Path,
    staging_directory: Path,
) -> None:
    """Commit successful group states after Outlook accepts the email.

    Existing states are backed up and restored if a later replacement fails.
    If this function fails after Outlook accepted the message, the next run may
    send a duplicate rather than risk losing an update.
    """
    successful = [result for result in results if result.succeeded]
    live_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=str(live_directory), prefix=".email-state-backup-"
    ) as backup_name:
        backup_directory = Path(backup_name)
        originally_present = set()
        replaced = []
        for result in successful:
            live_file = live_directory / result.state_file.name
            if live_file.is_file():
                shutil.copy2(live_file, backup_directory / live_file.name)
                originally_present.add(live_file.name)

        try:
            for result in successful:
                candidate = staging_directory / result.state_file.name
                live_file = live_directory / result.state_file.name
                _atomic_copy(candidate, live_file)
                replaced.append(live_file)
        except (OSError, RuntimeError) as exc:
            rollback_errors = []
            for live_file in reversed(replaced):
                try:
                    if live_file.name in originally_present:
                        _atomic_copy(
                            backup_directory / live_file.name,
                            live_file,
                        )
                    else:
                        live_file.unlink(missing_ok=True)
                except (OSError, RuntimeError) as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            extra = ""
            if rollback_errors:
                extra = " Rollback warning: " + "; ".join(rollback_errors)
            raise RuntimeError(
                "The email was submitted, but saved-history files could not "
                f"be committed. A later run may resend updates. {exc}{extra}"
            ) from exc


def print_group_list() -> None:
    print("Available regulatory source groups:")
    for group in GROUPS:
        print(f"  {group.number}. {group.sources}")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run Groups 1-7 and print one combined regulatory-updates report."
        )
    )
    parser.add_argument(
        "--groups",
        type=_parse_groups,
        default=tuple(group.number for group in GROUPS),
        metavar="LIST",
        help=(
            "groups to run: 'all' or comma-separated numbers "
            "(default: all)"
        ),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=default_state_directory(),
        help=(
            "folder containing the seven saved-history files "
            f"(default: {default_state_directory()})"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            f"request timeout passed to every group "
            f"(default: {DEFAULT_TIMEOUT_SECONDS:g} seconds)"
        ),
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        help="optionally save the combined report as a UTF-8 text file",
    )
    parser.add_argument(
        "--send-email",
        action="store_true",
        help=(
            "save and send the full report through the current Microsoft "
            "Outlook profile"
        ),
    )
    parser.add_argument(
        "--email-config",
        type=Path,
        default=default_email_config_path(),
        help=(
            "JSON file containing recipients and email preferences "
            f"(default: {default_email_config_path()})"
        ),
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        help=(
            "folder for automatically saved emailed reports; defaults to "
            "the folder saved in email settings, or "
            f"{default_reports_directory()}"
        ),
    )
    parser.add_argument(
        "--email-log",
        type=Path,
        default=default_email_log_path(),
        help=(
            "delivery audit log used by email mode "
            f"(default: {default_email_log_path()})"
        ),
    )
    parser.add_argument(
        "--validate-email-config",
        action="store_true",
        help="validate the email settings without checking websites or sending",
    )
    parser.add_argument(
        "--list-groups",
        action="store_true",
        help="show the seven groups and exit",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    configure_console_encoding()
    arguments = build_argument_parser().parse_args(argv)
    if arguments.list_groups:
        print_group_list()
        return 0
    if arguments.validate_email_config:
        try:
            settings = load_email_settings(arguments.email_config.resolve())
            print(
                "Email settings are valid. "
                f"Configured recipients: {len(settings.recipients)}."
            )
            return 0
        except EmailConfigurationError as exc:
            print(f"Email configuration error: {exc}", file=sys.stderr)
            return 2
    if arguments.timeout <= 0:
        print("Error: --timeout must be greater than zero.", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    live_state_directory = arguments.state_dir.resolve()
    settings: Optional[EmailSettings] = None
    try:
        if arguments.send_email:
            settings = load_email_settings(arguments.email_config.resolve())
            live_state_directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                dir=str(live_state_directory),
                prefix=".email-state-staging-",
            ) as staging_name:
                staging_directory = Path(staging_name)
                _copy_live_states_to_staging(
                    group_numbers=arguments.groups,
                    live_directory=live_state_directory,
                    staging_directory=staging_directory,
                )
                results = run_selected_groups(
                    group_numbers=arguments.groups,
                    state_directory=staging_directory,
                    timeout=arguments.timeout,
                )
                finished_at = datetime.now(timezone.utc)
                report = build_combined_report(
                    results=results,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                print(report, end="")
                _validate_staged_states(results, staging_directory)

                if arguments.report_file:
                    report_path = arguments.report_file.resolve()
                else:
                    if arguments.reports_dir is not None:
                        reports_directory = arguments.reports_dir.resolve()
                    elif settings.report_directory:
                        reports_directory = Path(
                            settings.report_directory
                        ).expanduser().resolve()
                    else:
                        reports_directory = default_reports_directory()
                    report_path = timestamped_report_path(
                        reports_directory, finished_at
                    )
                save_report(report_path, report)
                print(f"\nReport saved to: {report_path}")

                subject = build_email_subject(
                    settings=settings,
                    results=results,
                    finished_at=finished_at,
                )
                try:
                    send_report_via_outlook(
                        settings=settings,
                        subject=subject,
                        report=report,
                        report_path=report_path,
                    )
                except EmailDeliveryError as exc:
                    try:
                        append_email_log(
                            arguments.email_log.resolve(),
                            "FAILED",
                            f"Report: {report_path}. Error: {exc}",
                        )
                    except OSError as log_exc:
                        print(
                            f"Warning: could not write email log: {log_exc}",
                            file=sys.stderr,
                        )
                    raise

                try:
                    commit_emailed_states(
                        results=results,
                        live_directory=live_state_directory,
                        staging_directory=staging_directory,
                    )
                except RuntimeError as exc:
                    try:
                        append_email_log(
                            arguments.email_log.resolve(),
                            "SENT_STATE_COMMIT_FAILED",
                            f"Report: {report_path}. Error: {exc}",
                        )
                    except OSError as log_exc:
                        print(
                            f"Warning: could not write email log: {log_exc}",
                            file=sys.stderr,
                        )
                    raise
                try:
                    append_email_log(
                        arguments.email_log.resolve(),
                        "SENT",
                        (
                            f"Report: {report_path}. "
                            f"Recipients: {len(settings.recipients)}."
                        ),
                    )
                except OSError as log_exc:
                    print(
                        f"Warning: could not write email log: {log_exc}",
                        file=sys.stderr,
                    )
                print(
                    "Email submitted to Outlook successfully for "
                    f"{len(settings.recipients)} recipient(s)."
                )
                return (
                    1
                    if any(not result.succeeded for result in results)
                    else 0
                )

        results = run_selected_groups(
            group_numbers=arguments.groups,
            state_directory=live_state_directory,
            timeout=arguments.timeout,
        )
        finished_at = datetime.now(timezone.utc)
        report = build_combined_report(
            results=results,
            started_at=started_at,
            finished_at=finished_at,
        )
        print(report, end="")
        if arguments.report_file:
            report_path = arguments.report_file.resolve()
            save_report(report_path, report)
            print(f"\nReport saved to: {report_path}")
        return 1 if any(not result.succeeded for result in results) else 0
    except (
        EmailConfigurationError,
        EmailDeliveryError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
 
