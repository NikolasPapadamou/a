#!/usr/bin/env python3
"""Simple desktop interface for RegulatoryMonitor email settings and runs."""

from __future__ import annotations

import io
import queue
import re
import sys
import threading
import tkinter as tk
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, List, Optional, Tuple

import RegulatoryMonitor as monitor
from email_delivery import (
    DEFAULT_SUBJECT_PREFIX,
    EmailConfigurationError,
    EmailDeliveryError,
    EmailSettings,
    list_outlook_sender_accounts,
    load_email_settings,
    save_email_settings,
    send_report_via_outlook,
    validate_email_address,
)


DEFAULT_SENDER_LABEL = "Use Outlook default account"


class RegulatoryMonitorApplication:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config_path = monitor.default_email_config_path()
        self.running = False
        self.action_buttons: List[ttk.Button] = []
        self.worker_results: "queue.Queue[Tuple[str, Any, Any]]" = queue.Queue()

        self.root.title("Regulatory Updates Monitor")
        self.root.geometry("900x860")
        self.root.minsize(760, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._close_application)

        self.sender_var = tk.StringVar(value=DEFAULT_SENDER_LABEL)
        self.subject_var = tk.StringVar(value=DEFAULT_SUBJECT_PREFIX)
        self.attach_report_var = tk.BooleanVar(value=True)
        self.report_directory_var = tk.StringVar(
            value=str(monitor.default_reports_directory())
        )
        self.recipient_entry_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.group_vars = {
            group.number: tk.BooleanVar(value=True) for group in monitor.GROUPS
        }

        self._configure_style()
        self._build_interface()
        self._load_existing_settings()
        self.root.after(100, self._poll_worker_results)
        self.root.after(250, lambda: self._refresh_outlook_accounts(False))

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10))
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))

    def _build_interface(self) -> None:
        main = ttk.Frame(self.root, padding=18)
        main.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)

        ttk.Label(
            main,
            text="Regulatory Updates Monitor",
            style="Title.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            main,
            text=(
                "Manage report recipients and send the complete regulatory "
                "report through Microsoft Outlook."
            ),
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 14))

        email_frame = ttk.LabelFrame(
            main,
            text="Report and email settings",
            style="Section.TLabelframe",
            padding=12,
        )
        email_frame.grid(row=2, column=0, sticky="ew")
        email_frame.columnconfigure(1, weight=1)

        ttk.Label(email_frame, text="Send from:").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=4
        )
        self.sender_combo = ttk.Combobox(
            email_frame,
            textvariable=self.sender_var,
            state="readonly",
            values=(DEFAULT_SENDER_LABEL,),
        )
        self.sender_combo.grid(row=0, column=1, sticky="ew", pady=4)
        self.sender_combo.bind("<<ComboboxSelected>>", self._sender_changed)
        refresh_button = ttk.Button(
            email_frame,
            text="Refresh accounts",
            command=lambda: self._refresh_outlook_accounts(True),
        )
        refresh_button.grid(row=0, column=2, padx=(8, 0), pady=4)
        self.action_buttons.append(refresh_button)

        self.sender_help_label = ttk.Label(
            email_frame,
            text=(
                "Uses Outlook's default sending account for this Windows "
                "profile. No Outlook password is stored."
            ),
            foreground="#555555",
        )
        self.sender_help_label.grid(
            row=1, column=1, columnspan=2, sticky="w", pady=(0, 8)
        )

        ttk.Label(email_frame, text="Add recipient:").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=4
        )
        recipient_entry = ttk.Entry(
            email_frame,
            textvariable=self.recipient_entry_var,
        )
        recipient_entry.grid(row=2, column=1, sticky="ew", pady=4)
        recipient_entry.bind("<Return>", lambda event: self._add_recipients())
        add_button = ttk.Button(
            email_frame,
            text="Add",
            command=self._add_recipients,
        )
        add_button.grid(row=2, column=2, padx=(8, 0), pady=4)
        self.action_buttons.append(add_button)

        ttk.Label(email_frame, text="Recipients:").grid(
            row=3, column=0, sticky="nw", padx=(0, 10), pady=4
        )
        list_frame = ttk.Frame(email_frame)
        list_frame.grid(row=3, column=1, sticky="nsew", pady=4)
        list_frame.columnconfigure(0, weight=1)
        self.recipient_list = tk.Listbox(
            list_frame,
            height=4,
            selectmode=tk.EXTENDED,
            font=("Segoe UI", 10),
            exportselection=False,
        )
        recipient_scrollbar = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.recipient_list.yview,
        )
        self.recipient_list.configure(yscrollcommand=recipient_scrollbar.set)
        self.recipient_list.grid(row=0, column=0, sticky="nsew")
        recipient_scrollbar.grid(row=0, column=1, sticky="ns")
        remove_button = ttk.Button(
            email_frame,
            text="Remove selected",
            command=self._remove_recipients,
        )
        remove_button.grid(row=3, column=2, padx=(8, 0), pady=4, sticky="n")
        self.action_buttons.append(remove_button)

        ttk.Label(email_frame, text="Email subject:").grid(
            row=4, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(email_frame, textvariable=self.subject_var).grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=4
        )
        ttk.Checkbutton(
            email_frame,
            text="Attach the complete text report to the email",
            variable=self.attach_report_var,
        ).grid(row=5, column=1, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Label(email_frame, text="Save reports in:").grid(
            row=6, column=0, sticky="w", padx=(0, 10), pady=(8, 4)
        )
        ttk.Entry(
            email_frame,
            textvariable=self.report_directory_var,
        ).grid(row=6, column=1, sticky="ew", pady=(8, 4))
        browse_button = ttk.Button(
            email_frame,
            text="Browse...",
            command=self._choose_report_directory,
        )
        browse_button.grid(row=6, column=2, padx=(8, 0), pady=(8, 4))
        self.action_buttons.append(browse_button)

        group_frame = ttk.LabelFrame(
            main,
            text="Groups for a manual run",
            style="Section.TLabelframe",
            padding=10,
        )
        group_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        for column in range(4):
            group_frame.columnconfigure(column, weight=1)
        for index, group in enumerate(monitor.GROUPS):
            ttk.Checkbutton(
                group_frame,
                text=f"Group {group.number}",
                variable=self.group_vars[group.number],
            ).grid(
                row=index // 4,
                column=index % 4,
                sticky="w",
                padx=(0, 12),
                pady=2,
            )
        ttk.Button(
            group_frame,
            text="Select all",
            command=lambda: self._set_all_groups(True),
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Button(
            group_frame,
            text="Clear",
            command=lambda: self._set_all_groups(False),
        ).grid(row=2, column=1, sticky="w", pady=(6, 0))

        actions = ttk.Frame(main)
        actions.grid(row=4, column=0, sticky="ew", pady=12)
        save_button = ttk.Button(
            actions,
            text="Save settings",
            command=lambda: self._save_settings(True),
        )
        save_button.pack(side="left")
        test_button = ttk.Button(
            actions,
            text="Send test email",
            command=self._send_test_email,
        )
        test_button.pack(side="left", padx=(8, 0))
        email_run_button = ttk.Button(
            actions,
            text="Run and email report",
            command=self._run_and_email_report,
            style="Primary.TButton",
        )
        email_run_button.pack(side="right")
        save_run_button = ttk.Button(
            actions,
            text="Run and save the report",
            command=self._run_and_save_report,
        )
        save_run_button.pack(side="right", padx=(0, 8))
        self.action_buttons.extend(
            (save_button, test_button, save_run_button, email_run_button)
        )

        output_frame = ttk.LabelFrame(
            main,
            text="Run output",
            style="Section.TLabelframe",
            padding=8,
        )
        output_frame.grid(row=5, column=0, sticky="nsew")
        output_frame.rowconfigure(0, weight=1)
        output_frame.columnconfigure(0, weight=1)
        self.output_text = tk.Text(
            output_frame,
            wrap="word",
            height=12,
            font=("Consolas", 9),
            state="disabled",
        )
        output_scrollbar = ttk.Scrollbar(
            output_frame,
            orient="vertical",
            command=self.output_text.yview,
        )
        self.output_text.configure(yscrollcommand=output_scrollbar.set)
        self.output_text.grid(row=0, column=0, sticky="nsew")
        output_scrollbar.grid(row=0, column=1, sticky="ns")

        ttk.Label(
            main,
            textvariable=self.status_var,
            anchor="w",
            relief="sunken",
            padding=(6, 3),
        ).grid(row=6, column=0, sticky="ew", pady=(10, 0))

    def _load_existing_settings(self) -> None:
        if not self.config_path.exists():
            return
        try:
            settings = load_email_settings(
                self.config_path,
                require_recipients=False,
            )
        except EmailConfigurationError as exc:
            self.status_var.set(f"Could not load settings: {exc}")
            messagebox.showwarning("Email settings", str(exc), parent=self.root)
            return

        for recipient in settings.recipients:
            self.recipient_list.insert(tk.END, recipient)
        self.subject_var.set(settings.subject_prefix)
        self.attach_report_var.set(settings.attach_report)
        if settings.report_directory:
            self.report_directory_var.set(settings.report_directory)
        if settings.sender_account:
            self.sender_var.set(settings.sender_account)
            self.sender_combo.configure(
                values=(DEFAULT_SENDER_LABEL, settings.sender_account)
            )
        self._sender_changed()

    def _add_recipients(self) -> None:
        raw_value = self.recipient_entry_var.get().strip()
        if not raw_value:
            return
        candidates = [
            value for value in re.split(r"[;,\s]+", raw_value) if value
        ]
        existing = {
            str(self.recipient_list.get(index)).casefold()
            for index in range(self.recipient_list.size())
        }
        try:
            for candidate in candidates:
                address = validate_email_address(
                    candidate, "recipient email address"
                )
                if address.casefold() not in existing:
                    self.recipient_list.insert(tk.END, address)
                    existing.add(address.casefold())
        except EmailConfigurationError as exc:
            messagebox.showerror("Invalid email address", str(exc), parent=self.root)
            return
        self.recipient_entry_var.set("")
        self.status_var.set("Recipient list updated. Click Save settings.")

    def _remove_recipients(self) -> None:
        for index in reversed(self.recipient_list.curselection()):
            self.recipient_list.delete(index)
        self.status_var.set("Recipient list updated. Click Save settings.")

    def _choose_report_directory(self) -> None:
        current = self.report_directory_var.get().strip()
        initial_directory = current if Path(current).is_dir() else str(Path.home())
        selected = filedialog.askdirectory(
            parent=self.root,
            title="Choose where regulatory reports will be saved",
            initialdir=initial_directory,
            mustexist=False,
        )
        if selected:
            self.report_directory_var.set(str(Path(selected).resolve()))
            self.status_var.set(
                "Report folder updated. Click Save settings."
            )

    def _selected_sender(self) -> Optional[str]:
        value = self.sender_var.get().strip()
        if not value or value == DEFAULT_SENDER_LABEL:
            return None
        return value

    def _current_settings(self) -> EmailSettings:
        recipients = tuple(
            str(self.recipient_list.get(index))
            for index in range(self.recipient_list.size())
        )
        report_directory = self.report_directory_var.get().strip()
        if not report_directory:
            report_directory = str(monitor.default_reports_directory())
        report_directory = str(Path(report_directory).expanduser().resolve())
        return EmailSettings(
            recipients=recipients,
            subject_prefix=self.subject_var.get(),
            attach_report=bool(self.attach_report_var.get()),
            sender_account=self._selected_sender(),
            report_directory=report_directory,
        )

    def _save_settings(self, show_confirmation: bool) -> Optional[EmailSettings]:
        try:
            settings = self._current_settings()
            save_email_settings(
                self.config_path,
                settings,
                require_recipients=False,
            )
        except EmailConfigurationError as exc:
            messagebox.showerror("Cannot save settings", str(exc), parent=self.root)
            return None
        self.status_var.set(f"Settings saved to {self.config_path}")
        if show_confirmation:
            messagebox.showinfo(
                "Settings saved",
                "The report and email settings were saved successfully.",
                parent=self.root,
            )
        return settings

    def _require_email_recipients(
        self,
        settings: EmailSettings,
    ) -> bool:
        if settings.recipients:
            return True
        messagebox.showerror(
            "No email recipients",
            "Add at least one recipient before sending an email.",
            parent=self.root,
        )
        return False

    def _sender_changed(self, event: Optional[tk.Event] = None) -> None:
        sender = self._selected_sender()
        if sender:
            text = f"Reports will be sent from {sender}. No password is stored."
        else:
            text = (
                "Uses Outlook's default sending account for this Windows "
                "profile. No Outlook password is stored."
            )
        self.sender_help_label.configure(text=text)

    def _refresh_outlook_accounts(self, show_errors: bool) -> None:
        if self.running:
            return

        def worker() -> Tuple[str, ...]:
            return list_outlook_sender_accounts()

        def complete(accounts: Tuple[str, ...]) -> None:
            current = self.sender_var.get()
            values: List[str] = [DEFAULT_SENDER_LABEL]
            values.extend(accounts)
            if current not in values and current:
                values.append(current)
            self.sender_combo.configure(values=tuple(values))
            if current in values:
                self.sender_var.set(current)
            elif accounts:
                self.sender_var.set(DEFAULT_SENDER_LABEL)
            self._sender_changed()
            if accounts:
                self.status_var.set(
                    f"Found {len(accounts)} Outlook sending account(s)."
                )
            else:
                self.status_var.set(
                    "No SMTP Outlook accounts were found; the Outlook default "
                    "account can still be used."
                )

        self._start_worker(
            "Reading Outlook accounts...",
            worker,
            complete,
            show_errors=show_errors,
        )

    def _set_all_groups(self, selected: bool) -> None:
        for variable in self.group_vars.values():
            variable.set(selected)

    def _selected_groups(self) -> Tuple[int, ...]:
        return tuple(
            number
            for number, variable in self.group_vars.items()
            if variable.get()
        )

    def _send_test_email(self) -> None:
        settings = self._save_settings(False)
        if settings is None or not self._require_email_recipients(settings):
            return
        sender_text = settings.sender_account or "the Outlook default account"
        if not messagebox.askyesno(
            "Send test email",
            (
                f"Send a real test email to {len(settings.recipients)} "
                f"recipient(s) from {sender_text}?"
            ),
            parent=self.root,
        ):
            return

        test_settings = replace(settings, attach_report=False)

        def worker() -> None:
            send_report_via_outlook(
                settings=test_settings,
                subject=f"{settings.subject_prefix} - Test Email",
                report=(
                    "This is a test email from the Regulatory Updates Monitor.\n\n"
                    f"Submitted: {datetime.now().astimezone():%d/%m/%Y %H:%M:%S %Z}\n"
                    "No regulatory websites were checked during this test.\n"
                ),
            )

        def complete(result: Any) -> None:
            self.status_var.set("Test email submitted to Outlook successfully.")
            messagebox.showinfo(
                "Test email submitted",
                "Outlook accepted the test email for sending.",
                parent=self.root,
            )

        self._start_worker("Sending test email...", worker, complete)

    def _run_and_save_report(self) -> None:
        settings = self._save_settings(False)
        if settings is None:
            return
        selected_groups = self._selected_groups()
        if not selected_groups:
            messagebox.showerror(
                "No groups selected",
                "Select at least one regulatory group.",
                parent=self.root,
            )
            return

        reports_directory = Path(
            settings.report_directory or monitor.default_reports_directory()
        ).expanduser().resolve()
        report_path = monitor.timestamped_report_path(
            reports_directory,
            datetime.now(timezone.utc),
        )
        group_text = ", ".join(str(number) for number in selected_groups)
        if not messagebox.askyesno(
            "Run and save report",
            (
                f"Check Group(s) {group_text} and save the complete report "
                f"to:\n\n{report_path}\n\n"
                "No email will be sent. This can take several minutes."
            ),
            parent=self.root,
        ):
            return

        self._replace_output("Starting regulatory monitoring run...\n")

        def worker() -> Tuple[int, str]:
            standard_output = io.StringIO()
            error_output = io.StringIO()
            with redirect_stdout(standard_output), redirect_stderr(error_output):
                exit_code = monitor.main(
                    [
                        "--groups",
                        ",".join(str(number) for number in selected_groups),
                        "--report-file",
                        str(report_path),
                    ]
                )
            combined = standard_output.getvalue()
            errors = error_output.getvalue()
            if errors:
                combined = f"{combined}\n{errors}".strip() + "\n"
            return exit_code, combined

        def complete(result: Tuple[int, str]) -> None:
            exit_code, output = result
            self._replace_output(output)
            if exit_code == 0:
                self.status_var.set(f"Report saved to {report_path}")
                messagebox.showinfo(
                    "Report saved",
                    f"The complete report was saved to:\n\n{report_path}",
                    parent=self.root,
                )
            else:
                if report_path.is_file():
                    self.status_var.set(
                        f"Report saved with warnings to {report_path}"
                    )
                    warning = (
                        "The report was saved, but one or more groups had a "
                        f"problem.\n\nSaved to:\n{report_path}\n\n"
                        "Review the Run output section for details."
                    )
                else:
                    self.status_var.set(
                        "The run finished with a problem. Review the output below."
                    )
                    warning = "Review the Run output section for details."
                messagebox.showwarning(
                    "Run finished with a problem",
                    warning,
                    parent=self.root,
                )

        self._start_worker(
            "Checking regulatory sources and saving the report...",
            worker,
            complete,
        )

    def _run_and_email_report(self) -> None:
        settings = self._save_settings(False)
        if settings is None or not self._require_email_recipients(settings):
            return
        selected_groups = self._selected_groups()
        if not selected_groups:
            messagebox.showerror(
                "No groups selected",
                "Select at least one regulatory group.",
                parent=self.root,
            )
            return
        group_text = ", ".join(str(number) for number in selected_groups)
        if not messagebox.askyesno(
            "Run and email report",
            (
                f"Check Group(s) {group_text} and send the complete report "
                f"to {len(settings.recipients)} recipient(s)?\n\n"
                "This can take several minutes."
            ),
            parent=self.root,
        ):
            return

        self._replace_output("Starting regulatory monitoring run...\n")

        def worker() -> Tuple[int, str]:
            standard_output = io.StringIO()
            error_output = io.StringIO()
            with redirect_stdout(standard_output), redirect_stderr(error_output):
                exit_code = monitor.main(
                    [
                        "--groups",
                        ",".join(str(number) for number in selected_groups),
                        "--send-email",
                        "--email-config",
                        str(self.config_path),
                        "--reports-dir",
                        str(
                            Path(
                                settings.report_directory
                                or monitor.default_reports_directory()
                            ).expanduser().resolve()
                        ),
                    ]
                )
            combined = standard_output.getvalue()
            errors = error_output.getvalue()
            if errors:
                combined = f"{combined}\n{errors}".strip() + "\n"
            return exit_code, combined

        def complete(result: Tuple[int, str]) -> None:
            exit_code, output = result
            self._replace_output(output)
            if exit_code == 0:
                self.status_var.set(
                    "Monitoring completed and the report was submitted to Outlook."
                )
                messagebox.showinfo(
                    "Report submitted",
                    "The report was generated and submitted to Outlook successfully.",
                    parent=self.root,
                )
            else:
                report_saved = "Report saved to:" in output
                if report_saved:
                    self.status_var.set(
                        "Email delivery failed or the run was partial, but the "
                        "local report was saved."
                    )
                    warning = (
                        "The email was not fully successful, but the local "
                        "report was saved. Review the Run output section for "
                        "the exact path and error details."
                    )
                else:
                    self.status_var.set(
                        "The run finished with a problem. Review the output below."
                    )
                    warning = "Review the Run output section for details."
                messagebox.showwarning(
                    "Run finished with a problem",
                    warning,
                    parent=self.root,
                )

        self._start_worker(
            "Checking regulatory sources and preparing the email...",
            worker,
            complete,
        )

    def _start_worker(
        self,
        status: str,
        worker: Callable[[], Any],
        complete: Callable[[Any], None],
        show_errors: bool = True,
    ) -> None:
        if self.running:
            return
        self.running = True
        self.status_var.set(status)
        for button in self.action_buttons:
            button.configure(state="disabled")

        def run() -> None:
            try:
                result = worker()
            except Exception as exc:
                self.worker_results.put(("error", exc, show_errors))
            else:
                self.worker_results.put(("success", result, complete))

        threading.Thread(target=run, daemon=True).start()

    def _poll_worker_results(self) -> None:
        try:
            while True:
                result_type, value, callback = self.worker_results.get_nowait()
                if result_type == "success":
                    self._worker_done(value, callback)
                else:
                    self._worker_failed(value, bool(callback))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_worker_results)

    def _worker_done(self, result: Any, complete: Callable[[Any], None]) -> None:
        self.running = False
        for button in self.action_buttons:
            button.configure(state="normal")
        complete(result)

    def _worker_failed(self, error: Exception, show_error: bool) -> None:
        self.running = False
        for button in self.action_buttons:
            button.configure(state="normal")
        self.status_var.set(str(error))
        if show_error:
            messagebox.showerror("Operation failed", str(error), parent=self.root)

    def _replace_output(self, text: str) -> None:
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", text)
        self.output_text.configure(state="disabled")
        self.output_text.see(tk.END)

    def _close_application(self) -> None:
        if self.running:
            messagebox.showwarning(
                "Operation in progress",
                "Please wait for the current operation to finish before closing.",
                parent=self.root,
            )
            return
        self.root.destroy()


def _run_scheduled_mode(arguments: List[str]) -> int:
    """Run the email workflow without opening a window and retain its output."""
    monitor_arguments = [argument for argument in arguments if argument != "--scheduled"]
    if "--send-email" not in monitor_arguments:
        monitor_arguments.insert(0, "--send-email")

    standard_output = io.StringIO()
    error_output = io.StringIO()
    started = datetime.now().astimezone()
    try:
        with redirect_stdout(standard_output), redirect_stderr(error_output):
            exit_code = monitor.main(monitor_arguments)
    except Exception as exc:
        exit_code = 1
        error_output.write(
            f"Unexpected scheduled-mode {type(exc).__name__}: {exc}\n"
        )

    log_path = monitor._application_directory() / "logs" / "scheduled_task.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write("\n" + "=" * 72 + "\n")
            log_file.write(
                f"Scheduled run started: {started:%d/%m/%Y %H:%M:%S %Z}\n"
            )
            log_file.write(f"Exit code: {exit_code}\n\n")
            log_file.write(standard_output.getvalue())
            if error_output.getvalue():
                log_file.write("\nERROR OUTPUT\n------------\n")
                log_file.write(error_output.getvalue())
    except OSError:
        # A windowless scheduled task has nowhere else reliable to report a
        # logging failure. Its non-zero process result still reaches Scheduler.
        return 1
    return exit_code


def _run_packaged_self_test() -> int:
    """Check bundled runtime components without network or Outlook access."""
    root: Optional[tk.Tk] = None
    try:
        import certifi
        import pythoncom
        import win32com.client
        from curl_cffi import requests as curl_requests

        if not certifi.where() or curl_requests is None or pythoncom is None:
            raise RuntimeError("A required packaged dependency is unavailable.")
        root = tk.Tk()
        root.withdraw()
        RegulatoryMonitorApplication(root)
        root.update_idletasks()
        root.destroy()
        return 0
    except Exception as exc:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        log_path = monitor._application_directory() / "logs" / "self_test.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"Packaged self-test failed: {type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in arguments:
        return _run_packaged_self_test()
    if "--scheduled" in arguments:
        return _run_scheduled_mode(arguments)
    root = tk.Tk()
    RegulatoryMonitorApplication(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
