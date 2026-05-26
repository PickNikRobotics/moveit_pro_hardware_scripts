#!/usr/bin/env python3
"""Shared CD objective runner.

Connects to rosbridge, waits up to 1 hour for the /do_objective action server,
sends the objective goal, then exits. On timeout: stops the moveit-pro service
and posts a Slack failure notification using the same webhook as notify-crash.

Intended to be launched detached from CI over SSH; the calling shell can
exit immediately and the script will continue running on the host.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from threading import Event

import roslibpy
from roslibpy import ActionClient

ROSBRIDGE_HOST = "localhost"
ROSBRIDGE_PORT = 3201

ACTION_NAME = "/do_objective"
ACTION_TYPE = "moveit_studio_sdk_msgs/action/DoObjectiveSequence"
ACTION_STATUS_TOPIC = f"{ACTION_NAME}/_action/status"

TOTAL_TIMEOUT_S = 3600
POLL_INTERVAL_S = 10
ROSBRIDGE_CONNECT_TIMEOUT_S = 10
ROSAPI_CALL_TIMEOUT_S = 10
SEND_GOAL_DRAIN_S = 5

WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def _slack_post(message: str) -> None:
    if not WEBHOOK_URL:
        print(f"SLACK_WEBHOOK_URL not set; skipping notify: {message}", file=sys.stderr)
        return
    payload = {
        "date": time.strftime("%a %Y-%m-%d %H:%M:%S %Z"),
        "laptop_name": socket.gethostname(),
        "process_time": message,
    }
    try:
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=30)
        print(f"Slack notified: {message}")
    except Exception as exc:
        print(f"Slack notify failed: {exc}", file=sys.stderr)


def _stop_service() -> None:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        print("Cannot determine user for systemctl stop", file=sys.stderr)
        return
    unit = f"moveit-pro@{user}.service"
    print(f"Stopping {unit}")
    subprocess.run(
        ["sudo", "-n", "/bin/systemctl", "stop", unit],
        check=False,
    )


def _fail(reason: str):
    print(reason, file=sys.stderr)
    _slack_post(reason)
    _stop_service()
    sys.exit(1)


def _wait_for_rosbridge_port(deadline: float) -> None:
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        s = socket.socket()
        s.settimeout(2)
        try:
            if s.connect_ex((ROSBRIDGE_HOST, ROSBRIDGE_PORT)) == 0:
                print(f"Rosbridge port reachable (attempt {attempt})")
                return
        finally:
            s.close()
        print(f"Waiting for rosbridge port {ROSBRIDGE_PORT}... (attempt {attempt})")
        time.sleep(POLL_INTERVAL_S)
    _fail("Timeout: rosbridge port not reachable within 1h")


def _connect_rosbridge(client: roslibpy.Ros, deadline: float) -> None:
    try:
        client.run(timeout=ROSBRIDGE_CONNECT_TIMEOUT_S)
    except Exception as exc:
        print(f"Initial rosbridge run failed: {exc}")
    while time.monotonic() < deadline:
        if client.is_connected:
            print("Rosbridge websocket connected")
            return
        time.sleep(1)
    _fail("Timeout: rosbridge websocket did not connect within 1h")


def _get_topics(client: roslibpy.Ros) -> list:
    result = {"topics": None}
    done = Event()

    def _ok(resp):
        result["topics"] = resp.get("topics", []) if isinstance(resp, dict) else []
        done.set()

    def _err(err):
        print(f"rosapi topics call failed: {err}")
        done.set()

    client.get_topics(_ok, _err)
    if not done.wait(timeout=ROSAPI_CALL_TIMEOUT_S):
        return []
    return result["topics"] or []


def _wait_for_action_server(client: roslibpy.Ros, deadline: float) -> None:
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if ACTION_STATUS_TOPIC in _get_topics(client):
            print(f"{ACTION_NAME} action server is up (attempt {attempt})")
            return
        print(f"Waiting for {ACTION_NAME} action server... (attempt {attempt})")
        time.sleep(POLL_INTERVAL_S)
    _fail(f"Timeout: {ACTION_NAME} action server not ready within 1h")


def run_objective(objective_name: str) -> None:
    deadline = time.monotonic() + TOTAL_TIMEOUT_S
    client = roslibpy.Ros(host=ROSBRIDGE_HOST, port=ROSBRIDGE_PORT)

    try:
        _wait_for_rosbridge_port(deadline)
        _connect_rosbridge(client, deadline)
        _wait_for_action_server(client, deadline)

        action = ActionClient(client, ACTION_NAME, ACTION_TYPE)
        print(f"Sending objective: {objective_name}")
        action.send_goal(
            {"objective_name": objective_name},
            lambda r: None,
            lambda f: None,
            lambda e: None,
        )
        time.sleep(SEND_GOAL_DRAIN_S)
        print(f"Objective '{objective_name}' sent successfully")
    finally:
        if client.is_connected:
            client.terminate()


def _send_and_wait(
    action: ActionClient,
    objective_name: str,
    per_objective_timeout_s: float,
) -> None:
    """Send one objective goal and block until the action terminates.

    Any terminal status (success, abort, cancel) counts as completion of
    this iteration — we don't introspect the result payload here because
    DoObjectiveSequence's result shape is opaque to this script. Real
    crashes still get caught by the systemd notify-crash hook. We only
    fail() on rosbridge errors or timeouts.
    """
    done = Event()

    def _on_result(_result):
        done.set()

    def _on_feedback(_feedback):
        return

    def _on_error(err):
        print(f"Action error for '{objective_name}': {err}", file=sys.stderr)
        done.set()

    print(f"Sending objective: {objective_name}")
    action.send_goal(
        {"objective_name": objective_name},
        _on_result,
        _on_feedback,
        _on_error,
    )

    if not done.wait(timeout=per_objective_timeout_s):
        _fail(
            f"Timeout: objective '{objective_name}' did not terminate within "
            f"{per_objective_timeout_s:.0f}s"
        )
    print(f"Objective '{objective_name}' terminated")


def run_objectives_forever(
    objectives: list,
    iteration_pause_s: float = 5,
    per_objective_timeout_s: float = 3600,
) -> None:
    """Send each objective in `objectives` in order, wait for it to terminate,
    pause, then repeat — forever.

    Used by customer-config CD machines whose BT XML does not self-loop
    (Clean-Botix populate_mission_scene + test_change_tool, Auto Wash
    Test Run Job). Stuck objectives or rosbridge errors call _fail() which
    Slacks and stops the systemd unit; healthy iterations log and continue.
    """
    if not objectives:
        _fail("run_objectives_forever called with empty objectives list")

    deadline = time.monotonic() + TOTAL_TIMEOUT_S
    client = roslibpy.Ros(host=ROSBRIDGE_HOST, port=ROSBRIDGE_PORT)

    try:
        _wait_for_rosbridge_port(deadline)
        _connect_rosbridge(client, deadline)
        _wait_for_action_server(client, deadline)

        action = ActionClient(client, ACTION_NAME, ACTION_TYPE)
        iteration = 0
        while True:
            iteration += 1
            print(f"--- Iteration {iteration} ---")
            for objective_name in objectives:
                _send_and_wait(action, objective_name, per_objective_timeout_s)
            time.sleep(iteration_pause_s)
    finally:
        if client.is_connected:
            client.terminate()
