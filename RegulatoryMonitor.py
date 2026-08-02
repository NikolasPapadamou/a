#!/usr/bin/env python3
"""Run all seven regulatory-update groups and print one combined report."""

from __future__ import annotations

import argparse
import io
import os
import re
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
    if arguments.timeout <= 0:
        print("Error: --timeout must be greater than zero.", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    try:
        results = run_selected_groups(
            group_numbers=arguments.groups,
            state_directory=arguments.state_dir.resolve(),
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
            save_report(arguments.report_file.resolve(), report)
            print(
                f"\nReport saved to: {arguments.report_file.resolve()}"
            )
        return 1 if any(not result.succeeded for result in results) else 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
 