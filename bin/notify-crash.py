#!/usr/bin/env python3

import json
import os
import socket
import subprocess
import sys
import urllib.request

WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def get_payload_from_systemd(unit):
    # Check if the service exited with a failure.
    result = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "--property=ExecMainStatus,ActiveEnterTimestamp,ActiveExitTimestamp",
        ],
        capture_output=True,
        text=True,
    )

    props = {}
    for line in result.stdout.strip().splitlines():
        key, _, value = line.partition("=")
        props[key] = value

    exit_code = props.get("ExecMainStatus", "0")
    if exit_code == "0":
        return None

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

    return {
        "date": crash_time,
        "laptop_name": socket.gethostname(),
        "process_time": process_time,
    }


def get_dummy_payload():
    return {
        "date": "Sun 2026-04-13 14:19:47 MDT",
        "laptop_name": socket.gethostname(),
        "process_time": "2:34:12",
    }


def send(payload, dry_run=False):
    data = json.dumps(payload).encode()

    if dry_run:
        print(f"POST {WEBHOOK_URL or '<SLACK_WEBHOOK_URL not set>'}")
        print(json.dumps(payload, indent=2))
        return

    if not WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set; skipping notification", file=sys.stderr)
        return

    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)


def main():
    dry_run = "--dry-run" in sys.argv
    send_test = "--send" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--dry-run", "--send")]

    if dry_run or send_test:
        payload = get_dummy_payload()
        send(payload, dry_run=not send_test)
    else:
        unit = args[0] if args else "moveit-pro@unknown"
        payload = get_payload_from_systemd(unit)
        if payload is None:
            return
        send(payload)


if __name__ == "__main__":
    main()
