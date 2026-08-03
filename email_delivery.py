#!/usr/bin/env python3
"""Email configuration and Microsoft Outlook delivery helpers."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


DEFAULT_SUBJECT_PREFIX = "Regulatory Monitoring Report"
EMAIL_ADDRESS_PATTERN = re.compile(r"^[^\s@;,]+@[^\s@;,]+\.[^\s@;,]+$")


class EmailConfigurationError(ValueError):
    """Raised when the local email settings are missing or invalid."""


class EmailDeliveryError(RuntimeError):
    """Raised when Outlook cannot submit the report email."""


@dataclass(frozen=True)
class EmailSettings:
    recipients: Tuple[str, ...]
    subject_prefix: str = DEFAULT_SUBJECT_PREFIX
    attach_report: bool = True
    sender_account: Optional[str] = None


def validate_email_address(value: Any, field_name: str = "email address") -> str:
    if not isinstance(value, str) or not EMAIL_ADDRESS_PATTERN.fullmatch(
        value.strip()
    ):
        raise EmailConfigurationError(f"Invalid {field_name}: {value!r}.")
    return value.strip()


def _validate_recipients(
    value: Any,
    require_recipients: bool = True,
) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise EmailConfigurationError(
            "'recipients' must be a JSON list of email addresses."
        )

    recipients = []
    seen = set()
    for item in value:
        address = validate_email_address(item, "recipient email address")
        key = address.casefold()
        if key not in seen:
            recipients.append(address)
            seen.add(key)

    if require_recipients and not recipients:
        raise EmailConfigurationError(
            "Add at least one email address to 'recipients'."
        )
    return tuple(recipients)


def _validate_subject_prefix(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EmailConfigurationError(
            "'subject_prefix' must be a non-empty string."
        )
    subject_prefix = value.strip()
    if "\r" in subject_prefix or "\n" in subject_prefix:
        raise EmailConfigurationError(
            "'subject_prefix' cannot contain line breaks."
        )
    return subject_prefix


def _validate_sender_account(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return validate_email_address(value, "Outlook sender account")


def load_email_settings(
    path: Path,
    require_recipients: bool = True,
) -> EmailSettings:
    """Load and validate the non-secret email settings JSON file."""
    try:
        with path.open("r", encoding="utf-8") as settings_file:
            data: Dict[str, Any] = json.load(settings_file)
    except FileNotFoundError as exc:
        raise EmailConfigurationError(
            f"Email settings file was not found: {path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise EmailConfigurationError(
            f"Could not read email settings from {path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise EmailConfigurationError(
            "The email settings file must contain one JSON object."
        )

    recipients = _validate_recipients(
        data.get("recipients"),
        require_recipients=require_recipients,
    )
    subject_prefix = _validate_subject_prefix(
        data.get("subject_prefix", DEFAULT_SUBJECT_PREFIX)
    )

    attach_report = data.get("attach_report", True)
    if not isinstance(attach_report, bool):
        raise EmailConfigurationError(
            "'attach_report' must be true or false."
        )

    sender_account = _validate_sender_account(data.get("sender_account"))

    return EmailSettings(
        recipients=recipients,
        subject_prefix=subject_prefix,
        attach_report=attach_report,
        sender_account=sender_account,
    )


def save_email_settings(path: Path, settings: EmailSettings) -> None:
    """Validate and atomically save non-secret email preferences."""
    validated = EmailSettings(
        recipients=_validate_recipients(list(settings.recipients)),
        subject_prefix=_validate_subject_prefix(settings.subject_prefix),
        attach_report=settings.attach_report,
        sender_account=_validate_sender_account(settings.sender_account),
    )
    if not isinstance(validated.attach_report, bool):
        raise EmailConfigurationError("'attach_report' must be true or false.")

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
            json.dump(
                {
                    "recipients": list(validated.recipients),
                    "subject_prefix": validated.subject_prefix,
                    "attach_report": validated.attach_report,
                    "sender_account": validated.sender_account,
                },
                temporary_file,
                indent=2,
            )
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
        raise EmailConfigurationError(
            f"Could not save email settings to {path}: {exc}"
        ) from exc


def _outlook_dispatch() -> Callable[[str], Any]:
    try:
        from win32com.client import Dispatch
    except ImportError as exc:
        raise EmailDeliveryError(
            "Microsoft Outlook email support requires the pywin32 package. "
            "Install it with: python -m pip install -r requirements-email.txt"
        ) from exc
    return Dispatch


def _configured_outlook_accounts(outlook: Any) -> List[Any]:
    accounts = outlook.Session.Accounts
    return [accounts.Item(index) for index in range(1, accounts.Count + 1)]


def list_outlook_sender_accounts(
    dispatch: Optional[Callable[[str], Any]] = None,
) -> Tuple[str, ...]:
    """Return SMTP addresses configured in the current Outlook profile."""
    pythoncom = None
    if dispatch is None:
        try:
            import pythoncom as pythoncom_module
        except ImportError as exc:
            raise EmailDeliveryError(
                "Microsoft Outlook account discovery requires pywin32."
            ) from exc
        pythoncom = pythoncom_module
        pythoncom.CoInitialize()

    try:
        outlook_dispatch = dispatch or _outlook_dispatch()
        outlook = outlook_dispatch("Outlook.Application")
        addresses = []
        seen = set()
        for account in _configured_outlook_accounts(outlook):
            address = str(getattr(account, "SmtpAddress", "")).strip()
            if address and EMAIL_ADDRESS_PATTERN.fullmatch(address):
                key = address.casefold()
                if key not in seen:
                    addresses.append(address)
                    seen.add(key)
        return tuple(addresses)
    except EmailDeliveryError:
        raise
    except Exception as exc:
        raise EmailDeliveryError(
            "Outlook accounts could not be read: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if pythoncom is not None:
            pythoncom.CoUninitialize()


def _select_sender_account(outlook: Any, address: str) -> Any:
    for account in _configured_outlook_accounts(outlook):
        smtp_address = str(getattr(account, "SmtpAddress", "")).strip()
        if smtp_address.casefold() == address.casefold():
            return account
    raise EmailDeliveryError(
        f"The selected Outlook sender account is not available: {address}"
    )


def send_report_via_outlook(
    settings: EmailSettings,
    subject: str,
    report: str,
    report_path: Optional[Path] = None,
    dispatch: Optional[Callable[[str], Any]] = None,
) -> None:
    """Submit the complete report through the current Outlook profile.

    Outlook credentials are never requested or stored. A dispatch function can
    be injected by offline tests so they never contact a real Outlook profile.
    """
    if not settings.recipients:
        raise EmailDeliveryError("At least one email recipient is required.")
    if not subject.strip():
        raise EmailDeliveryError("The email subject cannot be empty.")
    if not report.strip():
        raise EmailDeliveryError("The email report cannot be empty.")
    if settings.attach_report:
        if report_path is None:
            raise EmailDeliveryError(
                "A saved report path is required when attachments are enabled."
            )
        if not report_path.is_file():
            raise EmailDeliveryError(
                f"The report attachment does not exist: {report_path}"
            )

    outlook_dispatch = dispatch or _outlook_dispatch()
    pythoncom = None
    if dispatch is None:
        try:
            import pythoncom as pythoncom_module
        except ImportError as exc:
            raise EmailDeliveryError(
                "Microsoft Outlook email support requires pywin32."
            ) from exc
        pythoncom = pythoncom_module
        pythoncom.CoInitialize()
    try:
        outlook = outlook_dispatch("Outlook.Application")
        message = outlook.CreateItem(0)
        message.To = "; ".join(settings.recipients)
        message.Subject = subject
        message.Body = report
        if settings.sender_account:
            message.SendUsingAccount = _select_sender_account(
                outlook, settings.sender_account
            )
        if settings.attach_report and report_path is not None:
            message.Attachments.Add(str(report_path.resolve()))
        message.Send()
    except EmailDeliveryError:
        raise
    except Exception as exc:
        raise EmailDeliveryError(
            "Outlook could not submit the regulatory report email: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if pythoncom is not None:
            pythoncom.CoUninitialize()
