from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import asr_admin_console as admin  # noqa: E402


class IssueFormTests(unittest.TestCase):
    def test_parse_issue_form(self) -> None:
        body = """### Scope

General — all repositories

### Target repositories

_No response_

### Announcement title

Quarterly review
"""
        parsed = admin.parse_issue_form(body)
        self.assertEqual(parsed["Scope"], "General — all repositories")
        self.assertEqual(parsed["Target repositories"], "")
        self.assertEqual(parsed["Announcement title"], "Quarterly review")

    def test_split_repository_targets(self) -> None:
        self.assertEqual(
            admin.split_repository_targets(
                "Alpha-Stochastic-Research/asr-open-sc, asr-theory-of-speculation"
            ),
            ["asr-open-sc", "asr-theory-of-speculation"],
        )


class RepositoryNameTests(unittest.TestCase):
    def test_normalize_repository_name(self) -> None:
        self.assertEqual(
            admin.normalize_repository_name(" ASR New Research "),
            "asr-new-research",
        )

    def test_reject_empty_repository_name(self) -> None:
        with self.assertRaises(ValueError):
            admin.normalize_repository_name("***")


class ProgressTests(unittest.TestCase):
    def test_missing_repository_is_proposed(self) -> None:
        metrics = admin.compute_project_metrics("Project", None)
        self.assertEqual(metrics.status, "Proposed")
        self.assertEqual(metrics.progress, 0)

    def test_completed_release_is_complete(self) -> None:
        repository = {
            "name": "asr-project",
            "html_url": "https://github.com/example/asr-project",
            "size": 100,
            "archived": False,
            "pushed_at": "2026-07-01T00:00:00Z",
        }
        metrics = admin.compute_project_metrics(
            "Project",
            repository,
            issue_total=4,
            issue_closed=4,
            pull_total=3,
            pull_merged=3,
            has_release=True,
            now=dt.datetime(2026, 7, 14, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(metrics.status, "Complete")
        self.assertEqual(metrics.progress, 100)

    def test_stale_open_work_is_blocked(self) -> None:
        repository = {
            "name": "asr-project",
            "html_url": "https://github.com/example/asr-project",
            "size": 100,
            "archived": False,
            "pushed_at": "2025-01-01T00:00:00Z",
        }
        metrics = admin.compute_project_metrics(
            "Project",
            repository,
            issue_total=5,
            issue_closed=1,
            pull_total=1,
            pull_merged=0,
            now=dt.datetime(2026, 7, 14, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(metrics.status, "Blocked")


class ProjectMatchingTests(unittest.TestCase):
    def test_explicit_link_wins(self) -> None:
        repositories = {
            "candidate": {"name": "candidate"},
            "explicit": {"name": "explicit"},
        }
        project = {
            "title": "Project",
            "repository_candidates": ["candidate"],
        }
        selected = admin.select_repository_for_project(
            project,
            repositories,
            {"Project": "explicit"},
        )
        self.assertEqual(selected["name"], "explicit")


class PermissionTests(unittest.TestCase):
    def test_repository_override(self) -> None:
        config = {
            "unknown_team_permission": "pull",
            "team_permissions": {"team": "push"},
            "repository_overrides": [
                {
                    "patterns": ["asr-admin-console"],
                    "permissions": {"team": "pull"},
                }
            ],
        }
        self.assertEqual(
            admin.permission_for("asr-admin-console", "team", config),
            "pull",
        )


if __name__ == "__main__":
    unittest.main()
