REGULATORY UPDATES MONITOR - PORTABLE WINDOWS APPLICATION
=========================================================

Keep the complete RegulatoryMonitor folder together. Do not copy only the EXE;
the _internal folder contains the Python runtime and libraries it needs.

INTERACTIVE USE
---------------
Double-click RegulatoryMonitor.exe.

1. Use Browse to choose where report files will be saved.
2. For local use, select groups and click Run and save the report. Recipients
   and Outlook are not required for this option.
3. For email use, add at least one recipient and click Add.
4. Choose the Outlook default sender or refresh and select an account.
5. Click Save settings and use Send test email before the first real report.
6. Select groups and click Run and email report.

The program uses the Microsoft Outlook desktop profile of the Windows user who
runs it. It does not store an Outlook password.

AUTOMATED USE (CONFIGURED LATER)
--------------------------------
Windows Task Scheduler should run:

    RegulatoryMonitor.exe --scheduled

This checks all seven groups and emails the full report using the recipients
saved through the interface. The user interface does not need to be open.

Keep the task configured for the Windows user whose Outlook profile sends the
message. The laptop must be running and able to access the internet and
Outlook. EY security policies may require the application to be approved.

SECURITY APPROVAL
-----------------
This development build is not digitally signed. Do not bypass an EY security
warning or attempt to disable endpoint protection. Ask the appropriate EY IT
or security team to approve, scan or sign the application before deploying it
to an EY-managed laptop.

DATA FOLDERS
------------
email_settings.json   Saved recipients and sender address (no password)
state\                 Update-history files created after monitoring runs
reports\               Saved text reports
logs\                  Email and Task Scheduler logs

FIRST RUN
---------
A fresh installation has no saved update history, so the first report may be
larger than later reports. Test the application and agree the initial baseline
before enabling its recurring scheduled task.
