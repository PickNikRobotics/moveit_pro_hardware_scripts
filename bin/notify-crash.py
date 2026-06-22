#!/usr/bin/env python3

import socket
import subprocess
import sys

sys.path.insert(0, "/usr/lib/moveit-pro-scripts")

try:
    from notify_lib import build_payload, github_issue, slack_post
except ImportError as exc:
    # ExecStopPost must never fail the service stop because a helper is missing.
    print(f"notify_lib unavailable, notifications disabled: {exc}", file=sys.stderr)

    def build_payload(process_time, date=None):
        return {"process_time": process_time}

    def slack_post(payload, dry_run=False):
        pass

    def github_issue(title, reason, version=None, dry_run=False):
        pass


# Bound the systemctl query so a hung call can't stall the service-stop path.
SYSTEMCTL_TIMEOUT_S = 10


def get_crash_info(unit):
    """Return (payload, reason) for a non-zero service exit, or (None, None).

    `payload` feeds Slack; `reason` is the human summary recorded on the
    GitHub issue. A clean exit (status 0) returns (None, None) so a normal
    `systemctl stop` does not notify.
    """
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                unit,
                "--property=ExecMainStatus,ActiveEnterTimestamp,ActiveExitTimestamp",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=SYSTEMCTL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # systemctl missing (container/test VM) or hung. Never block or raise
        # on the ExecStopPost path; just skip notification.
        print(f"systemctl unavailable, skipping crash notify: {exc}", file=sys.stderr)
        return None, None

    props = {}
    for line in result.stdout.strip().splitlines():
        key, _, value = line.partition("=")
        props[key] = value

    exit_code = props.get("ExecMainStatus", "0")
    if exit_code == "0":
        return None, None

    crash_time = props.get("ActiveExitTimestamp", "unknown")
    start_time = props.get("ActiveEnterTimestamp", "unknown")

    if start_time != "unknown" and crash_time != "unknown":
        from datetime import datetime

        fmt = "%a %Y-%m-%d %H:%M:%S %Z"
        try:
            dt_start = datetime.strptime(start_time, fmt)
            dt_crash = datetime.strptime(crash_time, fmt)
            process_time = str(dt_crash - dt_start)
        except ValueError:
            process_time = f"from {start_time} to {crash_time}"
    else:
        process_time = "unknown"

    payload = build_payload(process_time, date=crash_time)
    reason = f"Service {unit} exited with status {exit_code} (uptime {process_time})"
    return payload, reason


def main():
    dry_run = "--dry-run" in sys.argv
    send_test = "--send" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--dry-run", "--send")]
    unit = args[0] if args else "moveit-pro@unknown"
    title = f"QA deployment crash: {socket.gethostname()}"

    if dry_run or send_test:
        payload = build_payload("2:34:12", date="Sun 2026-04-13 14:19:47 MDT")
        reason = f"Test crash notification for {unit}"
        slack_post(payload, dry_run=not send_test)
        # Distinct title so a --send test never dedupes into the real crash
        # issue stream.
        test_title = f"QA deployment crash test: {socket.gethostname()}"
        github_issue(test_title, reason, dry_run=not send_test)
        return

    payload, reason = get_crash_info(unit)
    if payload is None:
        return
    slack_post(payload)
    github_issue(title, reason)


if __name__ == "__main__":
    main()
