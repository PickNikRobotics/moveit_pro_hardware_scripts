#!/usr/bin/env python3
"""Tests for notify_lib pure functions and GitHub-issue dedup logic.

Network-free: GitHub REST is stubbed so the dedup branch (create vs. bump vs.
re-seed a hand-edited body) is exercised deterministically. Run with:

    python3 -m unittest discover -s test
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import notify_lib  # noqa: E402


class TestNextLink(unittest.TestCase):
    """_next_link parses the rel="next" target out of a Link header."""

    def test_returns_next_url(self) -> None:
        header = (
            '<https://api.github.com/repos/o/r/issues?page=2>; rel="next", '
            '<https://api.github.com/repos/o/r/issues?page=5>; rel="last"'
        )
        self.assertEqual(
            notify_lib._next_link(header),
            "https://api.github.com/repos/o/r/issues?page=2",
        )

    def test_comma_inside_url_does_not_truncate(self) -> None:
        header = '<https://api.github.com/x?labels=a,b&page=2>; rel="next"'
        self.assertEqual(
            notify_lib._next_link(header),
            "https://api.github.com/x?labels=a,b&page=2",
        )

    def test_no_next_returns_none(self) -> None:
        header = '<https://api.github.com/repos/o/r/issues?page=5>; rel="last"'
        self.assertIsNone(notify_lib._next_link(header))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(notify_lib._next_link(""))


class TestSanitizeCell(unittest.TestCase):
    """_sanitize_cell keeps text on one Markdown table cell."""

    def test_strips_table_breaking_chars(self) -> None:
        self.assertEqual(
            notify_lib._sanitize_cell("a|b\nc`d "),
            "a/b c'd",
        )


class TestBuildPayload(unittest.TestCase):
    """build_payload shapes the Slack body for both call sites."""

    def test_explicit_date_preserved(self) -> None:
        payload = notify_lib.build_payload("reason X", date="D")
        self.assertEqual(payload["date"], "D")
        self.assertEqual(payload["process_time"], "reason X")
        self.assertIn("laptop_name", payload)

    def test_default_date_filled(self) -> None:
        payload = notify_lib.build_payload("reason X")
        self.assertTrue(payload["date"])


@mock.patch.dict(os.environ, {"MOVEIT_CD_GITHUB_TOKEN": "t"})
@mock.patch.object(notify_lib, "installed_version", return_value="9.9.9")
@mock.patch.object(notify_lib.socket, "gethostname", return_value="qa-host")
class TestGithubIssueDedup(unittest.TestCase):
    """github_issue creates, bumps, or re-seeds depending on the existing body."""

    def test_creates_new_issue_when_none_exists(self, _host, _ver) -> None:
        with (
            mock.patch.object(notify_lib, "_gh_list_open_issues", return_value=[]),
            mock.patch.object(
                notify_lib, "_gh_request", return_value=({"number": 1}, "")
            ) as req,
        ):
            notify_lib.github_issue("QA deployment crash: qa-host", "boom")

        method, url, _token, body = req.call_args.args
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/issues"))
        self.assertEqual(body["labels"], ["qa-deployment-failure"])
        self.assertIn("**Occurrences:** 1", body["body"])
        self.assertIn("| 1 | `9.9.9` | `qa-host` |", body["body"])

    def test_bumps_existing_counter(self, _host, _ver) -> None:
        existing = {
            "number": 7,
            "title": "QA deployment crash: qa-host",
            "body": "intro\n\n**Occurrences:** 3\n\n| # |\n|---|\n| 3 | x |\n",
        }
        with (
            mock.patch.object(
                notify_lib, "_gh_list_open_issues", return_value=[existing]
            ),
            mock.patch.object(
                notify_lib, "_gh_request", return_value=(None, "")
            ) as req,
        ):
            notify_lib.github_issue("QA deployment crash: qa-host", "boom again")

        patch_call = req.call_args_list[0]
        self.assertEqual(patch_call.args[0], "PATCH")
        patched_body = patch_call.args[3]["body"]
        self.assertIn("**Occurrences:** 4", patched_body)
        self.assertNotIn("**Occurrences:** 3", patched_body)
        self.assertIn("| 4 | `9.9.9` | `qa-host` |", patched_body)
        # A visibility comment is posted on the second call.
        self.assertEqual(req.call_args_list[1].args[0], "POST")
        self.assertIn("occurrence #4", req.call_args_list[1].args[3]["body"])

    @mock.patch.dict(os.environ, {"MOVEIT_CD_ISSUE_REPO": "../../user"})
    def test_invalid_repo_is_rejected_before_any_request(self, _host, _ver) -> None:
        with mock.patch.object(notify_lib, "_gh_request") as req:
            notify_lib.github_issue("QA deployment crash: qa-host", "boom")
        req.assert_not_called()

    def test_never_raises_when_lookup_explodes(self, _host, _ver) -> None:
        # An unexpected error in the work path must be contained, not propagate
        # onto the systemd service-stop path.
        with mock.patch.object(
            notify_lib, "_gh_list_open_issues", side_effect=RuntimeError("boom")
        ):
            notify_lib.github_issue("QA deployment crash: qa-host", "boom")

    def test_reseeds_counter_when_missing(self, _host, _ver) -> None:
        existing = {
            "number": 9,
            "title": "QA deployment crash: qa-host",
            "body": "someone deleted the counter line\n\n| # |\n|---|\n| 1 | x |\n",
        }
        with (
            mock.patch.object(
                notify_lib, "_gh_list_open_issues", return_value=[existing]
            ),
            mock.patch.object(
                notify_lib, "_gh_request", return_value=(None, "")
            ) as req,
        ):
            notify_lib.github_issue("QA deployment crash: qa-host", "boom")

        patched_body = req.call_args_list[0].args[3]["body"]
        self.assertIn("**Occurrences:** 2", patched_body)


if __name__ == "__main__":
    unittest.main()
