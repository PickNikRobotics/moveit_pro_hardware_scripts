#!/usr/bin/env python3
"""Shared failure-notification helpers for QA deployment hardware.

Two call sites fire on a QA deployment failure:

  * notify-crash.py        -- systemd ExecStopPost on non-zero service exit.
  * cd_objective_lib.py    -- objective-runner timeout / rosbridge failure.

Both post to Slack (SLACK_WEBHOOK_URL) and, when a token is configured, open
or update a deduplicated GitHub issue on the MoveIt Pro repo. Every function
here is best-effort: a notification failure must never propagate and break the
service-stop path that called it.

Environment (read from /etc/default/moveit-pro via the systemd unit):
  SLACK_WEBHOOK_URL        -- Slack incoming webhook. Unset -> Slack skipped.
  MOVEIT_CD_GITHUB_TOKEN   -- fine-grained PAT, Issues:RW on the issue repo.
                              Unset -> GitHub issue creation skipped (this is
                              how non-QA machines opt out).
  MOVEIT_CD_ISSUE_REPO     -- "owner/repo" for issues. Default below.
  MOVEIT_CD_ISSUE_LABEL    -- dedup label. Default below.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

WEBHOOK_URL_ENV = "SLACK_WEBHOOK_URL"
GITHUB_TOKEN_ENV = "MOVEIT_CD_GITHUB_TOKEN"
ISSUE_REPO_ENV = "MOVEIT_CD_ISSUE_REPO"
ISSUE_LABEL_ENV = "MOVEIT_CD_ISSUE_LABEL"

DEFAULT_ISSUE_REPO = "PickNikRobotics/moveit_pro"
DEFAULT_ISSUE_LABEL = "qa-deployment-failure"

# Debian package name installed by install-moveit-pro.
PACKAGE_NAME = "moveit-pro"

GITHUB_API = "https://api.github.com"
# Kept well under systemd's default TimeoutStopSec (90s): this runs on the
# service-stop path, and a slow API must not eat the whole stop budget.
HTTP_TIMEOUT_S = 10
# Safety ceiling on issue-list pagination so a malformed Link header can never
# loop forever on the stop path. 20 pages * 100 = far beyond any real backlog.
MAX_ISSUE_PAGES = 20

# owner/repo slug, matching GitHub's own naming constraint. Guards against an
# env value like "../../user" steering requests to other API endpoints.
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _env(name, default=""):
    return os.environ.get(name, default)


def installed_version():
    """Return the installed MoveIt Pro package version, or "unknown".

    Queried from dpkg so the issue records exactly which build failed.
    """
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", PACKAGE_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
        version = result.stdout.strip()
        return version or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def build_payload(process_time, date=None):
    """Build the Slack payload shared by both call sites.

    `process_time` carries either an uptime duration (crash) or a failure
    reason (objective runner). `date` defaults to now; notify-crash passes the
    systemd crash timestamp instead.
    """
    return {
        "date": time.strftime("%a %Y-%m-%d %H:%M:%S %Z") if date is None else date,
        "laptop_name": socket.gethostname(),
        "process_time": process_time,
    }


def slack_post(payload, dry_run=False):
    """POST `payload` to the Slack webhook. Best-effort; never raises."""
    webhook = _env(WEBHOOK_URL_ENV)

    if dry_run:
        print(f"POST {webhook or '<SLACK_WEBHOOK_URL not set>'}")
        print(json.dumps(payload, indent=2))
        return

    if not webhook:
        print(
            f"{WEBHOOK_URL_ENV} not set; skipping Slack notification", file=sys.stderr
        )
        return

    # Broad catch on purpose: this runs on the service-stop path and must never
    # raise. json.dumps (TypeError) and urlopen (OSError) are both in scope.
    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S)
        print("Slack notified")
    except Exception as exc:
        print(f"Slack notify failed: {exc}", file=sys.stderr)


def _gh_request(method, url, token, body=None):
    """Issue an authenticated GitHub REST request; return (parsed JSON, Link
    header string), or (None, "") on failure. The Link header is read inside
    the response context so callers never touch a closed response. Never
    raises. (urllib.error.URLError is an OSError subclass, so OSError covers
    network errors, HTTP errors, and timeouts; ValueError covers JSON decode.)
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "moveit-pro-hardware-scripts")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode()
            link = resp.headers.get("Link", "")
        parsed = json.loads(raw) if raw else None
        return parsed, link
    except (OSError, ValueError) as exc:
        print(f"GitHub API {method} {url} failed: {exc}", file=sys.stderr)
        return None, ""


def _gh_list_open_issues(repo, label, token):
    """Return all open issues carrying `label`, following pagination."""
    issues = []
    url = (
        f"{GITHUB_API}/repos/{repo}/issues"
        f"?state=open&labels={urllib.parse.quote(label)}&per_page=100"
    )
    pages = 0
    while url:
        pages += 1
        if pages > MAX_ISSUE_PAGES:
            print(
                f"Issue pagination exceeded {MAX_ISSUE_PAGES} pages; stopping",
                file=sys.stderr,
            )
            break
        parsed, link = _gh_request("GET", url, token)
        if parsed is None:
            break
        # The issues endpoint also returns PRs; they carry pull_request and
        # never our label, but filter defensively.
        issues.extend(i for i in parsed if "pull_request" not in i)
        url = _next_link(link)
    return issues


_NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*[^,]*rel="next"')


def _next_link(link_header):
    """Extract the rel="next" URL from an RFC 5988 Link header, or None.

    Matches the bracketed URL directly rather than splitting on "," so a comma
    inside a URL cannot corrupt the parse and silently truncate pagination.
    """
    match = _NEXT_LINK_RE.search(link_header)
    return match.group(1) if match else None


def _sanitize_cell(text):
    """Make `text` safe for a one-line Markdown cell/value.

    Strips newlines and pipes (table-breaking), neutralizes backticks (code
    spans), and escapes brackets so a crafted reason/hostname cannot render as
    a Markdown link.
    """
    return (
        text.replace("\n", " ")
        .replace("|", "/")
        .replace("`", "'")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .strip()
    )


def github_issue(title, reason, version=None, dry_run=False):
    """Open or update a deduplicated GitHub issue for a QA deployment failure.

    Dedup is by exact title within the configured label. An existing open issue
    has its occurrence counter bumped, a new table row appended, and a comment
    posted for notification visibility; otherwise a fresh issue is created.

    Skipped silently when no token is configured (non-QA machines).
    """
    token = _env(GITHUB_TOKEN_ENV)
    repo = _env(ISSUE_REPO_ENV, DEFAULT_ISSUE_REPO)
    label = _env(ISSUE_LABEL_ENV, DEFAULT_ISSUE_LABEL)
    version = version or installed_version()
    hostname = socket.gethostname()
    when = time.strftime("%a %Y-%m-%d %H:%M:%S %Z")
    reason_cell = _sanitize_cell(reason)

    if dry_run:
        print(f"GitHub issue on {repo} (label {label}): {title}")
        print(f"  version={version} host={hostname} reason={reason_cell}")
        return

    if not token:
        print(
            f"{GITHUB_TOKEN_ENV} not set; skipping GitHub issue: {title}",
            file=sys.stderr,
        )
        return

    if not _REPO_RE.match(repo):
        print(
            f"Invalid {ISSUE_REPO_ENV} '{repo}'; skipping GitHub issue: {title}",
            file=sys.stderr,
        )
        return

    # Belt to _gh_request's suspenders: enforce the never-raise contract at the
    # function boundary so a regex / int / response-shape surprise on the
    # service-stop path cannot propagate.
    try:
        _do_github_issue(
            title, reason_cell, version, hostname, when, repo, label, token
        )
    except Exception as exc:
        print(f"GitHub issue creation failed: {exc}", file=sys.stderr)


def _do_github_issue(title, reason_cell, version, hostname, when, repo, label, token):
    """Look up the deduplicated issue and create or update it.

    May raise; the public github_issue() wrapper contains it.
    """
    existing = None
    for issue in _gh_list_open_issues(repo, label, token):
        if issue.get("title") == title:
            existing = issue
            break

    # Build the row with a plain f-string (no .format) so a "{" or "}" in any
    # field can never raise KeyError / corrupt the row.
    def _row(n):
        return f"| {n} | `{version}` | `{hostname}` | {when} | {reason_cell} |"

    if existing is None:
        occurrence = 1
        body = (
            f"A QA deployment failed on `{hostname}`.\n\n"
            f"**Reason:** {reason_cell}\n"
            f"**Occurrences:** {occurrence}\n\n"
            "| # | Version | Machine | Time | Reason |\n"
            "|---|---------|---------|------|--------|\n"
            f"{_row(occurrence)}\n"
        )
        created, _ = _gh_request(
            "POST",
            f"{GITHUB_API}/repos/{repo}/issues",
            token,
            {"title": title, "body": body, "labels": [label]},
        )
        if created is not None:
            print(f"Opened GitHub issue #{created.get('number')}: {title}")
        return

    number = existing.get("number")
    body = existing.get("body") or ""
    count_match = re.search(r"\*\*Occurrences:\*\*\s*(\d+)", body)
    if count_match:
        occurrence = int(count_match.group(1)) + 1
        body = re.sub(
            r"\*\*Occurrences:\*\*\s*\d+",
            f"**Occurrences:** {occurrence}",
            body,
            count=1,
        )
    else:
        # Body lost its counter (e.g. hand-edited). Re-seed it so the count
        # keeps tracking instead of freezing on every later occurrence.
        occurrence = 2
        body = f"**Occurrences:** {occurrence}\n\n" + body
    body = body.rstrip("\n") + "\n" + _row(occurrence) + "\n"

    _gh_request(
        "PATCH",
        f"{GITHUB_API}/repos/{repo}/issues/{number}",
        token,
        {"body": body},
    )
    _gh_request(
        "POST",
        f"{GITHUB_API}/repos/{repo}/issues/{number}/comments",
        token,
        {
            "body": (
                f"QA deployment failed again (occurrence #{occurrence}).\n\n"
                f"**Version:** `{version}`\n"
                f"**Machine:** `{hostname}`\n"
                f"**Time:** {when}\n"
                f"**Reason:** {reason_cell}"
            )
        },
    )
    print(f"Updated GitHub issue #{number} (occurrence #{occurrence}): {title}")
