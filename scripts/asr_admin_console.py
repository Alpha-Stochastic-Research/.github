#!/usr/bin/env python3
"""Reconcile the ASR organization administration console.

The workflow is intentionally GitHub-native:

* create and continuously seed ``asr-admin-console``;
* accept announcement and repository requests through issue forms;
* publish one managed announcement digest in every active repository;
* create and maintain the organization ProjectV2 ``Incoming projects``;
* calculate project progress from repository activity;
* expose a generated Markdown dashboard in the admin repository.

Only the standard library is used so the automation can run on a stock
``ubuntu-latest`` GitHub Actions runner.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

API_ROOT = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"
API_VERSION = "2022-11-28"
USER_AGENT = "asr-admin-console-reconciler"
MAX_RETRIES = 5
MUTATION_DELAY_SECONDS = 0.75
ANNOUNCEMENT_DIGEST_TITLE = "[ASR] Announcements"
ANNOUNCEMENT_MARKER = "<!-- asr-announcement-digest:v1 -->"
PROJECT_ITEM_MARKER = "<!-- asr-incoming-project:v1 -->"
ACCESS_SETUP_TITLE = '[Setup] Restrict "Incoming projects" editing access'
VALID_REPOSITORY_PERMISSIONS = {"pull", "triage", "push", "maintain", "admin"}


class GitHubError(RuntimeError):
    """Raised for GitHub API failures."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_iso_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parse_iso_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    value = value.strip()
    if not value or value.lower() in {"_no response_", "none", "n/a", "na"}:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def compact_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_repository_name(value: str) -> str:
    """Normalize and validate a repository name supplied through an issue form."""

    value = value.strip().lower()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-z0-9._-]", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    if not value or len(value) > 100:
        raise ValueError("Repository name must contain between 1 and 100 characters.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
        raise ValueError("Repository name contains unsupported characters.")
    if value in {".", ".."}:
        raise ValueError("Repository name is reserved.")
    return value


def parse_issue_form(body: str | None) -> dict[str, str]:
    """Parse the Markdown emitted by GitHub issue forms.

    GitHub renders every input as a ``### Heading`` section followed by the
    submitted value. The parser intentionally ignores HTML comments and empty
    sections so it remains resilient to issue-form metadata changes.
    """

    if not body:
        return {}
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"^###\s+(.+?)\s*$", body, re.MULTILINE))
    for index, match in enumerate(matches):
        heading = compact_whitespace(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[start:end].strip()
        value = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL).strip()
        if value == "_No response_":
            value = ""
        sections[heading] = value
    return sections


def split_repository_targets(value: str) -> list[str]:
    targets: list[str] = []
    for raw in re.split(r"[\n,;]+", value or ""):
        candidate = raw.strip().strip("`")
        if not candidate:
            continue
        if "/" in candidate:
            candidate = candidate.rsplit("/", 1)[-1]
        targets.append(normalize_repository_name(candidate))
    return sorted(set(targets))


def permission_for(
    repository_name: str,
    team_slug: str,
    access_config: dict[str, Any],
) -> str:
    permission = access_config.get("team_permissions", {}).get(
        team_slug,
        access_config.get("unknown_team_permission", "pull"),
    )
    for override in access_config.get("repository_overrides", []):
        patterns = override.get("patterns", [])
        if any(fnmatch.fnmatchcase(repository_name, pattern) for pattern in patterns):
            permission = override.get("permissions", {}).get(team_slug, permission)
    if permission not in VALID_REPOSITORY_PERMISSIONS:
        raise ValueError(
            f"Invalid resolved permission {permission!r} for {team_slug!r}."
        )
    return permission


@dataclass(frozen=True)
class ProjectMetrics:
    title: str
    status: str
    progress: int
    repository: str | None
    repository_url: str | None
    last_activity: str | None
    note: str
    issue_total: int = 0
    issue_closed: int = 0
    pull_total: int = 0
    pull_merged: int = 0


def compute_project_metrics(
    title: str,
    repository: dict[str, Any] | None,
    *,
    issue_total: int = 0,
    issue_closed: int = 0,
    pull_total: int = 0,
    pull_merged: int = 0,
    has_release: bool = False,
    now: dt.datetime | None = None,
) -> ProjectMetrics:
    """Calculate a deterministic progress signal from repository activity."""

    now = now or utc_now()
    if repository is None:
        return ProjectMetrics(
            title=title,
            status="Proposed",
            progress=0,
            repository=None,
            repository_url=None,
            last_activity=None,
            note="Awaiting repository creation or linkage.",
        )

    pushed = parse_iso_datetime(repository.get("pushed_at"))
    last_activity = pushed.date().isoformat() if pushed else None
    size = int(repository.get("size") or 0)
    archived = bool(repository.get("archived"))

    completed = max(issue_closed, 0) + max(pull_merged, 0)
    total = max(issue_total, 0) + max(pull_total, 0)
    open_work = max(issue_total - issue_closed, 0) + max(pull_total - pull_merged, 0)

    progress = 5
    if size > 0:
        progress += 10
    if total > 0:
        progress += round(65 * min(completed / total, 1.0))
    if pull_merged > 0:
        progress += 10
    if has_release:
        progress += 10

    if archived:
        progress = 100
    elif has_release and open_work == 0:
        progress = 100
    else:
        progress = min(progress, 95)

    stale_days = (now - pushed).days if pushed else None
    if archived or progress == 100:
        status = "Complete"
    elif stale_days is not None and stale_days >= 120 and open_work > 0:
        status = "Blocked"
    elif progress >= 80:
        status = "Review"
    elif total > 0 or pull_merged > 0:
        status = "Active"
    else:
        status = "Scoping"

    details = [
        f"{issue_closed}/{issue_total} issues closed",
        f"{pull_merged}/{pull_total} pull requests merged",
    ]
    if has_release:
        details.append("release detected")
    if stale_days is not None:
        details.append(f"last push {stale_days} day(s) ago")

    return ProjectMetrics(
        title=title,
        status=status,
        progress=max(0, min(int(progress), 100)),
        repository=repository.get("name"),
        repository_url=repository.get("html_url"),
        last_activity=last_activity,
        note="; ".join(details) + ".",
        issue_total=issue_total,
        issue_closed=issue_closed,
        pull_total=pull_total,
        pull_merged=pull_merged,
    )


def select_repository_for_project(
    project: dict[str, Any],
    repositories: dict[str, dict[str, Any]],
    links: dict[str, str],
) -> dict[str, Any] | None:
    linked = links.get(project["title"])
    if linked and linked in repositories:
        return repositories[linked]
    for candidate in project.get("repository_candidates", []):
        if candidate in repositories:
            return repositories[candidate]
    return None


class GitHubClient:
    def __init__(self, token: str, label: str, *, dry_run: bool = False) -> None:
        self.token = token
        self.label = label
        self.dry_run = dry_run

    def request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | list[Any] | None = None,
        *,
        tolerate_404: bool = False,
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        for attempt in range(MAX_RETRIES + 1):
            request = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "X-GitHub-Api-Version": API_VERSION,
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    body = response.read()
                    return json.loads(body.decode("utf-8")) if body else None
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 404 and tolerate_404:
                    return None
                lower = body.lower()
                rate_limited = (
                    exc.code == 429
                    or (
                        exc.code == 403
                        and (
                            "rate limit" in lower
                            or "abuse detection" in lower
                            or "secondary rate" in lower
                        )
                    )
                )
                if rate_limited and attempt < MAX_RETRIES:
                    retry_after = exc.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        delay = max(int(retry_after), 1)
                    else:
                        delay = min(30 * (2**attempt), 240)
                    print(
                        f"[{self.label}] rate limited; retrying in {delay}s.",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                raise GitHubError(
                    f"{method} {url} failed with HTTP {exc.code}: {body}",
                    status=exc.code,
                ) from exc
            except urllib.error.URLError as exc:
                raise GitHubError(f"{method} {url} failed: {exc}") from exc
        raise GitHubError(f"{method} {url} failed after retries.")

    def rest(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | list[Any] | None = None,
        *,
        tolerate_404: bool = False,
    ) -> Any:
        return self.request(
            method,
            f"{API_ROOT}{path}",
            payload,
            tolerate_404=tolerate_404,
        )

    def graphql(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        tolerate_errors: bool = False,
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            GRAPHQL_URL,
            {"query": query, "variables": variables},
        )
        errors = response.get("errors") or []
        if errors and not tolerate_errors:
            raise GitHubError(f"GraphQL errors: {json.dumps(errors)}")
        return response

    def paginated_rest(self, path: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            batch = self.rest(
                "GET",
                f"{path}{separator}per_page=100&page={page}",
            )
            results.extend(batch)
            if len(batch) < 100:
                return results
            page += 1


class Reconciler:
    def __init__(
        self,
        config: dict[str, Any],
        access_config: dict[str, Any],
        template_root: Path,
        *,
        admin_token: str,
        project_token: str,
        dry_run: bool,
    ) -> None:
        self.config = config
        self.access_config = access_config
        self.template_root = template_root
        self.organization = config["organization"]
        self.admin_repository_name = config["admin_repository"]["name"]
        self.admin_repository_full_name = (
            f"{self.organization}/{self.admin_repository_name}"
        )
        self.dry_run = dry_run
        self.admin = GitHubClient(admin_token, "admin", dry_run=dry_run)
        self.projects = GitHubClient(project_token, "projects", dry_run=dry_run)
        self._teams: list[dict[str, Any]] | None = None
        self._repositories: list[dict[str, Any]] | None = None

    def log(self, message: str) -> None:
        print(message, flush=True)

    def mutate_rest(
        self,
        client: GitHubClient,
        method: str,
        path: str,
        payload: dict[str, Any] | list[Any] | None = None,
    ) -> Any:
        self.log(f"  {method} {path}")
        if self.dry_run:
            return None
        response = client.rest(method, path, payload)
        time.sleep(MUTATION_DELAY_SECONDS)
        return response

    def mutate_graphql(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        tolerate_errors: bool = False,
    ) -> dict[str, Any] | None:
        if self.dry_run:
            return None
        response = self.projects.graphql(
            query,
            variables,
            tolerate_errors=tolerate_errors,
        )
        time.sleep(MUTATION_DELAY_SECONDS)
        return response

    def list_teams(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._teams is None or refresh:
            org = urllib.parse.quote(self.organization, safe="")
            self._teams = self.admin.paginated_rest(f"/orgs/{org}/teams?")
        return self._teams

    def list_repositories(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._repositories is None or refresh:
            org = urllib.parse.quote(self.organization, safe="")
            repos = self.admin.paginated_rest(
                f"/orgs/{org}/repos?type=all&sort=full_name&"
            )
            self._repositories = [
                repo
                for repo in repos
                if not repo.get("archived") and not repo.get("disabled")
            ]
        return self._repositories

    def get_repository(self, name: str) -> dict[str, Any] | None:
        org = urllib.parse.quote(self.organization, safe="")
        repo = urllib.parse.quote(name, safe="")
        return self.admin.rest(
            "GET",
            f"/repos/{org}/{repo}",
            tolerate_404=True,
        )

    def ensure_admin_repository(self) -> dict[str, Any]:
        settings = self.config["admin_repository"]
        repository = self.get_repository(self.admin_repository_name)
        if repository is None:
            self.log(f"Create repository {self.admin_repository_full_name}")
            payload = {
                "name": self.admin_repository_name,
                "description": settings["description"],
                "visibility": settings.get("visibility", "private"),
                "auto_init": True,
                "has_issues": True,
                "has_projects": True,
                "has_discussions": True,
                "delete_branch_on_merge": True,
            }
            created = self.mutate_rest(
                self.admin,
                "POST",
                f"/orgs/{urllib.parse.quote(self.organization, safe='')}/repos",
                payload,
            )
            if self.dry_run:
                return {
                    "name": self.admin_repository_name,
                    "full_name": self.admin_repository_full_name,
                    "default_branch": settings.get("default_branch", "main"),
                    "html_url": (
                        f"https://github.com/{self.admin_repository_full_name}"
                    ),
                    "visibility": settings.get("visibility", "private"),
                }
            repository = created
            self._repositories = None

        patch = {
            "name": self.admin_repository_name,
            "description": settings["description"],
            "has_issues": True,
            "has_projects": True,
            "has_discussions": True,
            "delete_branch_on_merge": True,
        }
        needs_patch = any(
            repository.get(key) != value
            for key, value in patch.items()
            if key != "name"
        )
        if needs_patch:
            self.mutate_rest(
                self.admin,
                "PATCH",
                f"/repos/{self.admin_repository_full_name}",
                patch,
            )
        topic_response = self.admin.rest(
            "GET",
            f"/repos/{self.admin_repository_full_name}/topics",
        )
        desired_topics = sorted(settings.get("topics", []))
        if sorted(topic_response.get("names", [])) != desired_topics:
            self.mutate_rest(
                self.admin,
                "PUT",
                f"/repos/{self.admin_repository_full_name}/topics",
                {"names": desired_topics},
            )
        return repository

    def list_repository_teams(self, repository_name: str) -> dict[str, str]:
        teams = self.admin.paginated_rest(
            f"/repos/{self.organization}/{repository_name}/teams?"
        )
        current: dict[str, str] = {}
        for team in teams:
            role = team.get("role_name")
            if role in {"read", "write"}:
                role = {"read": "pull", "write": "push"}[role]
            if role not in VALID_REPOSITORY_PERMISSIONS:
                flags = team.get("permissions") or {}
                role = next(
                    (
                        candidate
                        for candidate in (
                            "admin",
                            "maintain",
                            "push",
                            "triage",
                            "pull",
                        )
                        if flags.get(candidate)
                    ),
                    "pull",
                )
            current[team["slug"]] = role
        return current

    def set_team_repository_permission(
        self,
        repository_name: str,
        team_slug: str,
        permission: str,
    ) -> None:
        org = urllib.parse.quote(self.organization, safe="")
        team = urllib.parse.quote(team_slug, safe="")
        repo = urllib.parse.quote(repository_name, safe="")
        self.mutate_rest(
            self.admin,
            "PUT",
            f"/orgs/{org}/teams/{team}/repos/{org}/{repo}",
            {"permission": permission},
        )

    def ensure_admin_repository_access(self) -> None:
        self.log("Reconcile asr-admin-console team permissions")
        current = self.list_repository_teams(self.admin_repository_name)
        editors = self.config["editor_teams"]
        default = self.config.get("all_other_team_permission", "pull")
        for team in self.list_teams():
            slug = team["slug"]
            expected = editors.get(slug, default)
            if current.get(slug) == expected:
                continue
            self.log(
                f"  team {slug}: {current.get(slug, 'not assigned')} -> {expected}"
            )
            self.set_team_repository_permission(
                self.admin_repository_name,
                slug,
                expected,
            )

    def ensure_labels(self, repository_name: str) -> None:
        existing = {
            label["name"]: label
            for label in self.admin.paginated_rest(
                f"/repos/{self.organization}/{repository_name}/labels?"
            )
        }
        for name, spec in self.config["labels"].items():
            current = existing.get(name)
            payload = {
                "name": name,
                "color": spec["color"],
                "description": spec.get("description", ""),
            }
            if current is None:
                self.mutate_rest(
                    self.admin,
                    "POST",
                    f"/repos/{self.organization}/{repository_name}/labels",
                    payload,
                )
            elif (
                current.get("color", "").lower() != spec["color"].lower()
                or (current.get("description") or "") != spec.get("description", "")
            ):
                self.mutate_rest(
                    self.admin,
                    "PATCH",
                    (
                        f"/repos/{self.organization}/{repository_name}/labels/"
                        f"{urllib.parse.quote(name, safe='')}"
                    ),
                    payload,
                )

    def get_file(
        self,
        repository_name: str,
        path: str,
        *,
        ref: str | None = None,
    ) -> dict[str, Any] | None:
        query = f"?ref={urllib.parse.quote(ref, safe='')}" if ref else ""
        return self.admin.rest(
            "GET",
            (
                f"/repos/{self.organization}/{repository_name}/contents/"
                f"{urllib.parse.quote(path, safe='/')}{query}"
            ),
            tolerate_404=True,
        )

    def read_json_file(
        self,
        repository_name: str,
        path: str,
        default: Any,
    ) -> Any:
        file_data = self.get_file(repository_name, path)
        if not file_data or file_data.get("type") != "file":
            return default
        encoded = file_data.get("content", "")
        try:
            raw = base64.b64decode(encoded).decode("utf-8")
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return default

    def upsert_file(
        self,
        repository_name: str,
        path: str,
        content: str,
        *,
        message: str,
        branch: str | None = None,
        create_only: bool = False,
    ) -> bool:
        existing = self.get_file(repository_name, path, ref=branch)
        if existing and create_only:
            return False
        if existing and existing.get("type") == "file":
            try:
                current = base64.b64decode(existing.get("content", "")).decode(
                    "utf-8"
                )
            except (ValueError, UnicodeDecodeError):
                current = ""
            if current == content:
                return False
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if branch:
            payload["branch"] = branch
        if existing:
            payload["sha"] = existing["sha"]
        self.mutate_rest(
            self.admin,
            "PUT",
            (
                f"/repos/{self.organization}/{repository_name}/contents/"
                f"{urllib.parse.quote(path, safe='/')}"
            ),
            payload,
        )
        return True

    def render_template(
        self,
        content: str,
        *,
        project_url: str,
    ) -> str:
        editor_lines = "\n".join(
            f"- `@{self.organization}/{slug}`"
            for slug in self.config["editor_teams"]
        )
        replacements = {
            "{{ORGANIZATION}}": self.organization,
            "{{ADMIN_REPOSITORY}}": self.admin_repository_name,
            "{{INCOMING_PROJECT_URL}}": project_url,
            "{{EDITOR_TEAMS}}": editor_lines,
            "{{SYNC_INTERVAL}}": str(self.config.get("schedule_minutes", 15)),
        }
        for marker, value in replacements.items():
            content = content.replace(marker, value)
        return content

    def ensure_template_files(
        self,
        repository: dict[str, Any],
        *,
        project_url: str,
    ) -> None:
        self.log("Reconcile admin console files")
        branch = repository.get("default_branch") or "main"
        for source in sorted(self.template_root.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(self.template_root).as_posix()
            raw = source.read_text(encoding="utf-8")
            rendered = self.render_template(raw, project_url=project_url)
            self.upsert_file(
                self.admin_repository_name,
                relative,
                rendered,
                message=f"Reconcile managed admin console file: {relative}",
                branch=branch,
                create_only=relative.startswith("data/"),
            )

    def organization_project(self) -> tuple[str, dict[str, Any] | None]:
        query = """
        query($organization: String!) {
          organization(login: $organization) {
            id
            projectsV2(first: 100) {
              nodes {
                id
                number
                title
                shortDescription
                readme
                public
                fields(first: 100) {
                  nodes {
                    ... on ProjectV2Field { id name dataType }
                    ... on ProjectV2SingleSelectField {
                      id
                      name
                      options { id name }
                    }
                  }
                }
                items(first: 100) {
                  nodes {
                    id
                    content {
                      ... on DraftIssue { title body }
                      ... on Issue { title body }
                      ... on PullRequest { title body }
                    }
                  }
                }
              }
            }
          }
        }
        """
        response = self.projects.graphql(
            query,
            {"organization": self.organization},
        )
        organization = response["data"]["organization"]
        if organization is None:
            raise GitHubError(f"Organization not found: {self.organization}")
        title = self.config["incoming_project"]["title"]
        project = next(
            (
                item
                for item in organization["projectsV2"]["nodes"]
                if item["title"] == title
            ),
            None,
        )
        return organization["id"], project

    def create_incoming_project(self, owner_id: str) -> dict[str, Any]:
        title = self.config["incoming_project"]["title"]
        self.log(f"Create organization project: {title}")
        mutation = """
        mutation($ownerId: ID!, $title: String!) {
          createProjectV2(input: {ownerId: $ownerId, title: $title}) {
            projectV2 { id number title }
          }
        }
        """
        response = self.mutate_graphql(
            mutation,
            {"ownerId": owner_id, "title": title},
        )
        if self.dry_run:
            return {"id": "DRY_RUN_PROJECT", "number": 0, "title": title}
        assert response is not None
        return response["data"]["createProjectV2"]["projectV2"]

    def update_project_metadata(self, project: dict[str, Any]) -> None:
        config = self.config["incoming_project"]
        readme = (
            "# Incoming projects\n\n"
            "Organization-wide research intake maintained by automation.\n\n"
            "## Access policy\n\n"
            "- Base role: **Read** for the organization.\n"
            "- Write access: **release-admins**, **ASR Leadership**, and "
            "**ASR Researchers** only.\n"
            "- The generated dashboard in `asr-admin-console` is the "
            "auditable source of calculated progress.\n\n"
            "## Automatic progress\n\n"
            "Progress uses repository existence, issue completion, merged "
            "pull requests, releases and recent push activity. Manual status "
            "changes may be reconciled on the next automation run."
        )
        changed = (
            project.get("shortDescription") != config["short_description"]
            or project.get("readme") != readme
            or project.get("public") != config.get("public", False)
        )
        if not changed:
            return
        mutation = """
        mutation(
          $projectId: ID!,
          $shortDescription: String!,
          $readme: String!,
          $public: Boolean!
        ) {
          updateProjectV2(input: {
            projectId: $projectId,
            shortDescription: $shortDescription,
            readme: $readme,
            public: $public
          }) { projectV2 { id } }
        }
        """
        self.log("Update Incoming projects metadata")
        self.mutate_graphql(
            mutation,
            {
                "projectId": project["id"],
                "shortDescription": config["short_description"],
                "readme": readme,
                "public": config.get("public", False),
            },
        )

    def create_project_field(
        self,
        project_id: str,
        name: str,
        data_type: str,
        *,
        options: list[dict[str, str]] | None = None,
    ) -> None:
        mutation = """
        mutation($input: CreateProjectV2FieldInput!) {
          createProjectV2Field(input: $input) {
            projectV2Field {
              ... on ProjectV2Field { id name }
              ... on ProjectV2SingleSelectField { id name }
            }
          }
        }
        """
        input_value: dict[str, Any] = {
            "projectId": project_id,
            "dataType": data_type,
            "name": name,
        }
        if options is not None:
            input_value["singleSelectOptions"] = options
        self.log(f"Create project field: {name}")
        self.mutate_graphql(mutation, {"input": input_value})

    def ensure_project_fields(self, project: dict[str, Any]) -> bool:
        fields = {
            field.get("name"): field
            for field in project.get("fields", {}).get("nodes", [])
            if field.get("name")
        }
        desired = [
            (
                "ASR Status",
                "SINGLE_SELECT",
                self.config["incoming_project"]["status_options"],
            ),
            ("Progress (%)", "NUMBER", None),
            ("Repository", "TEXT", None),
            ("Last activity", "DATE", None),
            ("Automation note", "TEXT", None),
        ]
        created = False
        for name, data_type, options in desired:
            if name in fields:
                continue
            self.create_project_field(
                project["id"],
                name,
                data_type,
                options=options,
            )
            created = True
        return created

    def add_project_item(
        self,
        project_id: str,
        title: str,
        body: str,
    ) -> None:
        mutation = """
        mutation($projectId: ID!, $title: String!, $body: String!) {
          addProjectV2DraftIssue(input: {
            projectId: $projectId,
            title: $title,
            body: $body
          }) {
            projectItem { id }
          }
        }
        """
        self.log(f"Add incoming project item: {title}")
        self.mutate_graphql(
            mutation,
            {"projectId": project_id, "title": title, "body": body},
        )

    def ensure_project_items(self, project: dict[str, Any]) -> bool:
        existing = {
            (node.get("content") or {}).get("title")
            for node in project.get("items", {}).get("nodes", [])
        }
        created = False
        for item in self.config["incoming_project"]["projects"]:
            title = item["title"]
            if title in existing:
                continue
            candidates = "\n".join(
                f"- `{candidate}`"
                for candidate in item.get("repository_candidates", [])
            )
            body = (
                f"{PROJECT_ITEM_MARKER}\n"
                "Managed by the ASR admin-console automation.\n\n"
                "## Expected repository names\n\n"
                f"{candidates or '- To be assigned'}\n\n"
                "Progress and status are calculated from linked repository "
                "activity."
            )
            self.add_project_item(project["id"], title, body)
            created = True
        return created

    def update_project_field(
        self,
        project_id: str,
        item_id: str,
        field_id: str,
        value: dict[str, Any],
    ) -> None:
        mutation = """
        mutation($input: UpdateProjectV2ItemFieldValueInput!) {
          updateProjectV2ItemFieldValue(input: $input) {
            projectV2Item { id }
          }
        }
        """
        self.mutate_graphql(
            mutation,
            {
                "input": {
                    "projectId": project_id,
                    "itemId": item_id,
                    "fieldId": field_id,
                    "value": value,
                }
            },
        )

    def link_project_to_repository(
        self,
        project_id: str,
        repository_node_id: str,
    ) -> None:
        mutation = """
        mutation($projectId: ID!, $repositoryId: ID!) {
          linkProjectV2ToRepository(input: {
            projectId: $projectId,
            repositoryId: $repositoryId
          }) { clientMutationId }
        }
        """
        response = self.mutate_graphql(
            mutation,
            {"projectId": project_id, "repositoryId": repository_node_id},
            tolerate_errors=True,
        )
        if response and response.get("errors"):
            messages = " ".join(
                str(error.get("message", "")) for error in response["errors"]
            ).lower()
            if "already" not in messages and "link" not in messages:
                raise GitHubError(
                    f"Unable to link project to repository: {response['errors']}"
                )

    def link_project_to_team(self, project_id: str, team_node_id: str) -> None:
        mutation = """
        mutation($projectId: ID!, $teamId: ID!) {
          linkProjectV2ToTeam(input: {
            projectId: $projectId,
            teamId: $teamId
          }) { clientMutationId }
        }
        """
        response = self.mutate_graphql(
            mutation,
            {"projectId": project_id, "teamId": team_node_id},
            tolerate_errors=True,
        )
        if response and response.get("errors"):
            messages = " ".join(
                str(error.get("message", "")) for error in response["errors"]
            ).lower()
            if "already" not in messages and "link" not in messages:
                self.log(
                    "  warning: project/team link could not be established: "
                    f"{response['errors']}"
                )

    def query_count(self, query: str) -> int:
        encoded = urllib.parse.urlencode({"q": query})
        response = self.admin.rest("GET", f"/search/issues?{encoded}&per_page=1")
        return int(response.get("total_count") or 0)

    def repository_has_release(self, repository_name: str) -> bool:
        response = self.admin.rest(
            "GET",
            f"/repos/{self.organization}/{repository_name}/releases/latest",
            tolerate_404=True,
        )
        return response is not None

    def calculate_all_metrics(
        self,
        links: dict[str, str],
    ) -> list[ProjectMetrics]:
        repositories = {
            repo["name"]: repo for repo in self.list_repositories(refresh=True)
        }
        metrics: list[ProjectMetrics] = []
        for project in self.config["incoming_project"]["projects"]:
            repository = select_repository_for_project(project, repositories, links)
            if repository is None:
                metrics.append(compute_project_metrics(project["title"], None))
                continue
            name = repository["name"]
            issue_total = self.query_count(
                f"repo:{self.organization}/{name} is:issue"
            )
            issue_closed = self.query_count(
                f"repo:{self.organization}/{name} is:issue is:closed"
            )
            pull_total = self.query_count(
                f"repo:{self.organization}/{name} is:pr"
            )
            pull_merged = self.query_count(
                f"repo:{self.organization}/{name} is:pr is:merged"
            )
            has_release = self.repository_has_release(name)
            metrics.append(
                compute_project_metrics(
                    project["title"],
                    repository,
                    issue_total=issue_total,
                    issue_closed=issue_closed,
                    pull_total=pull_total,
                    pull_merged=pull_merged,
                    has_release=has_release,
                )
            )
        return metrics

    @staticmethod
    def metrics_snapshot(metrics: Iterable[ProjectMetrics]) -> dict[str, Any]:
        return {
            metric.title: {
                "status": metric.status,
                "progress": metric.progress,
                "repository": metric.repository,
                "last_activity": metric.last_activity,
                "note": metric.note,
            }
            for metric in metrics
        }

    def sync_project_fields(
        self,
        project: dict[str, Any],
        metrics: list[ProjectMetrics],
        previous_snapshot: dict[str, Any],
    ) -> None:
        fields = {
            field.get("name"): field
            for field in project.get("fields", {}).get("nodes", [])
            if field.get("name")
        }
        items = {
            (node.get("content") or {}).get("title"): node
            for node in project.get("items", {}).get("nodes", [])
            if (node.get("content") or {}).get("title")
        }
        status_field = fields.get("ASR Status")
        if not status_field:
            raise GitHubError("ASR Status field was not created.")
        status_options = {
            option["name"]: option["id"]
            for option in status_field.get("options", [])
        }
        required_fields = {
            name: fields.get(name)
            for name in (
                "Progress (%)",
                "Repository",
                "Last activity",
                "Automation note",
            )
        }
        if any(value is None for value in required_fields.values()):
            raise GitHubError("One or more Incoming projects fields are missing.")

        for metric in metrics:
            node = items.get(metric.title)
            if not node:
                continue
            before = previous_snapshot.get(metric.title, {})
            item_id = node["id"]
            changes: list[tuple[str, dict[str, Any]]] = []
            if before.get("status") != metric.status:
                option_id = status_options.get(metric.status)
                if option_id:
                    changes.append(
                        (
                            status_field["id"],
                            {"singleSelectOptionId": option_id},
                        )
                    )
            if before.get("progress") != metric.progress:
                changes.append(
                    (
                        required_fields["Progress (%)"]["id"],
                        {"number": float(metric.progress)},
                    )
                )
            repository_value = metric.repository or "Not assigned"
            if before.get("repository") != metric.repository:
                changes.append(
                    (
                        required_fields["Repository"]["id"],
                        {"text": repository_value},
                    )
                )
            if (
                metric.last_activity
                and before.get("last_activity") != metric.last_activity
            ):
                changes.append(
                    (
                        required_fields["Last activity"]["id"],
                        {"date": metric.last_activity},
                    )
                )
            if before.get("note") != metric.note:
                changes.append(
                    (
                        required_fields["Automation note"]["id"],
                        {"text": metric.note[:1024]},
                    )
                )
            for field_id, value in changes:
                self.log(f"  update project field: {metric.title}")
                self.update_project_field(
                    project["id"],
                    item_id,
                    field_id,
                    value,
                )

    def build_dashboard(
        self,
        project_url: str,
        metrics: list[ProjectMetrics],
    ) -> str:
        rows = []
        for metric in metrics:
            if metric.repository_url:
                repository = f"[`{metric.repository}`]({metric.repository_url})"
            else:
                repository = "—"
            last_activity = metric.last_activity or "—"
            rows.append(
                f"| {metric.title} | {metric.status} | "
                f"{metric.progress}% | {repository} | {last_activity} |"
            )
        return (
            "<!-- asr-incoming-projects-dashboard:v1 -->\n"
            "# Incoming projects\n\n"
            f"[Open the GitHub Project]({project_url})\n\n"
            "Progress is calculated from linked repository activity: repository "
            "existence, issue completion, merged pull requests, releases and "
            "recent pushes.\n\n"
            "| Project | Status | Progress | Repository | Last activity |\n"
            "|---|---:|---:|---|---|\n"
            + "\n".join(rows)
            + "\n\n"
            "## Access policy\n\n"
            "All organization teams receive read access to this repository. "
            "Only `release-admins`, `asr-leadership`, and `asr-researchers` "
            "receive write access. GitHub Project collaborator roles require "
            "the one-time access setup issue generated in this repository.\n"
        )

    def ensure_incoming_project(
        self,
    ) -> tuple[dict[str, Any], str, list[ProjectMetrics]]:
        owner_id, project = self.organization_project()
        if project is None:
            project = self.create_incoming_project(owner_id)
            if self.dry_run:
                url = (
                    f"https://github.com/orgs/{self.organization}/projects"
                )
                metrics = self.calculate_all_metrics({})
                return project, url, metrics
            owner_id, project = self.organization_project()
            assert project is not None

        self.update_project_metadata(project)
        changed = self.ensure_project_fields(project)
        changed = self.ensure_project_items(project) or changed
        if changed and not self.dry_run:
            _, refreshed = self.organization_project()
            assert refreshed is not None
            project = refreshed

        admin_repo = self.get_repository(self.admin_repository_name)
        if admin_repo and admin_repo.get("node_id"):
            self.link_project_to_repository(project["id"], admin_repo["node_id"])
        for team in self.list_teams():
            node_id = team.get("node_id")
            if node_id:
                self.link_project_to_team(project["id"], node_id)

        project_url = (
            f"https://github.com/orgs/{self.organization}/projects/"
            f"{project['number']}"
        )
        links = self.read_json_file(
            self.admin_repository_name,
            "data/repository-project-links.json",
            {},
        )
        if not isinstance(links, dict):
            links = {}
        previous = self.read_json_file(
            self.admin_repository_name,
            "data/incoming-projects-status.json",
            {},
        )
        if not isinstance(previous, dict):
            previous = {}

        metrics = self.calculate_all_metrics(links)
        self.sync_project_fields(project, metrics, previous)
        snapshot = self.metrics_snapshot(metrics)
        self.upsert_file(
            self.admin_repository_name,
            "data/incoming-projects-status.json",
            json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
            message="Refresh Incoming projects status snapshot",
            branch=(admin_repo or {}).get("default_branch", "main"),
        )
        self.upsert_file(
            self.admin_repository_name,
            "INCOMING_PROJECTS.md",
            self.build_dashboard(project_url, metrics),
            message="Refresh Incoming projects dashboard",
            branch=(admin_repo or {}).get("default_branch", "main"),
        )
        return project, project_url, metrics

    def issue_labels(self, issue: dict[str, Any]) -> set[str]:
        return {
            label["name"] if isinstance(label, dict) else str(label)
            for label in issue.get("labels", [])
        }

    def list_issues(
        self,
        repository_name: str,
        *,
        state: str = "open",
        labels: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params = {"state": state, "sort": "created", "direction": "asc"}
        if labels:
            params["labels"] = ",".join(labels)
        query = urllib.parse.urlencode(params)
        issues = self.admin.paginated_rest(
            f"/repos/{self.organization}/{repository_name}/issues?{query}&"
        )
        return [issue for issue in issues if "pull_request" not in issue]

    def add_issue_labels(
        self,
        repository_name: str,
        issue_number: int,
        labels: list[str],
    ) -> None:
        self.mutate_rest(
            self.admin,
            "POST",
            (
                f"/repos/{self.organization}/{repository_name}/issues/"
                f"{issue_number}/labels"
            ),
            {"labels": labels},
        )

    def comment_issue(
        self,
        repository_name: str,
        issue_number: int,
        body: str,
    ) -> None:
        self.mutate_rest(
            self.admin,
            "POST",
            (
                f"/repos/{self.organization}/{repository_name}/issues/"
                f"{issue_number}/comments"
            ),
            {"body": body},
        )

    def close_issue(
        self,
        repository_name: str,
        issue_number: int,
    ) -> None:
        self.mutate_rest(
            self.admin,
            "PATCH",
            (
                f"/repos/{self.organization}/{repository_name}/issues/"
                f"{issue_number}"
            ),
            {"state": "closed", "state_reason": "completed"},
        )

    def team_member_is_active(self, team_slug: str, username: str) -> bool:
        org = urllib.parse.quote(self.organization, safe="")
        team = urllib.parse.quote(team_slug, safe="")
        user = urllib.parse.quote(username, safe="")
        membership = self.admin.rest(
            "GET",
            f"/orgs/{org}/teams/{team}/memberships/{user}",
            tolerate_404=True,
        )
        return bool(membership and membership.get("state") == "active")

    def actor_in_any_team(self, username: str, teams: Iterable[str]) -> bool:
        return any(
            self.team_member_is_active(team_slug, username)
            for team_slug in teams
        )

    def create_repository_from_request(
        self,
        *,
        name: str,
        description: str,
        visibility: str,
        template_name: str,
    ) -> dict[str, Any]:
        templates = self.config.get("repository_templates", {})
        template_repository = templates.get(template_name)
        if template_name not in templates:
            raise ValueError(f"Unknown starting point: {template_name}")
        if template_repository:
            payload = {
                "owner": self.organization,
                "name": name,
                "description": description,
                "private": visibility != "public",
                "include_all_branches": False,
            }
            result = self.mutate_rest(
                self.admin,
                "POST",
                (
                    f"/repos/{self.organization}/{template_repository}/generate"
                ),
                payload,
            )
        else:
            payload = {
                "name": name,
                "description": description,
                "visibility": visibility,
                "auto_init": True,
                "has_issues": True,
                "has_projects": True,
                "has_discussions": True,
                "delete_branch_on_merge": True,
            }
            result = self.mutate_rest(
                self.admin,
                "POST",
                f"/orgs/{self.organization}/repos",
                payload,
            )
        if self.dry_run:
            return {
                "name": name,
                "html_url": f"https://github.com/{self.organization}/{name}",
                "default_branch": "main",
                "node_id": "DRY_RUN_REPOSITORY",
            }
        self.mutate_rest(
            self.admin,
            "PATCH",
            f"/repos/{self.organization}/{name}",
            {
                "description": description,
                "has_issues": True,
                "has_projects": True,
                "has_discussions": True,
                "delete_branch_on_merge": True,
            },
        )
        self.mutate_rest(
            self.admin,
            "PUT",
            f"/repos/{self.organization}/{name}/topics",
            {"names": ["asr", "managed-repository"]},
        )
        assert result is not None
        self._repositories = None
        return result

    def apply_repository_access_policy(self, repository_name: str) -> None:
        current = self.list_repository_teams(repository_name)
        for team in self.list_teams():
            slug = team["slug"]
            expected = permission_for(repository_name, slug, self.access_config)
            if current.get(slug) != expected:
                self.set_team_repository_permission(
                    repository_name,
                    slug,
                    expected,
                )

    def update_project_link_mapping(
        self,
        project_title: str,
        repository_name: str,
    ) -> None:
        if not project_title or project_title.lower() in {
            "none",
            "not linked",
            "_no response_",
        }:
            return
        valid_titles = {
            item["title"] for item in self.config["incoming_project"]["projects"]
        }
        if project_title not in valid_titles:
            raise ValueError(
                f"Unknown Incoming projects item: {project_title!r}"
            )
        path = "data/repository-project-links.json"
        links = self.read_json_file(self.admin_repository_name, path, {})
        if not isinstance(links, dict):
            links = {}
        if links.get(project_title) == repository_name:
            return
        links[project_title] = repository_name
        repository = self.get_repository(self.admin_repository_name) or {}
        self.upsert_file(
            self.admin_repository_name,
            path,
            json.dumps(links, indent=2, ensure_ascii=False) + "\n",
            message=f"Link {project_title} to {repository_name}",
            branch=repository.get("default_branch", "main"),
        )

    def process_repository_requests(self) -> int:
        self.log("Process repository creation requests")
        created_count = 0
        issues = self.list_issues(
            self.admin_repository_name,
            labels=["asr-repository-request"],
        )
        for issue in issues:
            labels = self.issue_labels(issue)
            if labels & {"asr-processed", "asr-rejected"}:
                continue
            if created_count >= self.config.get(
                "max_repository_creations_per_run", 5
            ):
                break
            number = issue["number"]
            actor = issue["user"]["login"]
            form = parse_issue_form(issue.get("body"))
            team_slug = compact_whitespace(
                form.get("Requesting team slug", "")
            ).lower()
            try:
                if not team_slug or not self.team_member_is_active(
                    team_slug, actor
                ):
                    raise PermissionError(
                        "The requester must be an active member of the "
                        "declared organization team."
                    )
                repository_name = normalize_repository_name(
                    form.get("Repository name", "")
                )
                if repository_name in {".github", self.admin_repository_name}:
                    raise ValueError("This repository name is reserved.")
                description = compact_whitespace(
                    form.get("Repository description", "")
                )
                if len(description) < 12:
                    raise ValueError(
                        "Repository description must contain at least 12 "
                        "characters."
                    )
                visibility_raw = compact_whitespace(
                    form.get("Visibility", "Private")
                ).lower()
                visibility = {
                    "private": "private",
                    "public": "public",
                    "internal": "internal",
                }.get(visibility_raw)
                if visibility is None:
                    raise ValueError("Visibility must be Private, Internal or Public.")
                template_name = compact_whitespace(
                    form.get("Starting point", "Blank repository")
                )
                related_project = compact_whitespace(
                    form.get("Related incoming project", "")
                )
                existing = self.get_repository(repository_name)
                if existing:
                    repository = existing
                    outcome = "already existed"
                else:
                    repository = self.create_repository_from_request(
                        name=repository_name,
                        description=description,
                        visibility=visibility,
                        template_name=template_name,
                    )
                    self.apply_repository_access_policy(repository_name)
                    self.ensure_labels(repository_name)
                    created_count += 1
                    outcome = "was created"
                self.update_project_link_mapping(
                    related_project,
                    repository_name,
                )
                self.comment_issue(
                    self.admin_repository_name,
                    number,
                    (
                        f"✅ Repository [{self.organization}/{repository_name}]"
                        f"({repository['html_url']}) {outcome}.\n\n"
                        f"- Requesting team: `@{self.organization}/{team_slug}`\n"
                        f"- Visibility: `{visibility}`\n"
                        f"- Starting point: `{template_name}`\n"
                        "- Organization access policy, labels, announcements "
                        "and project tracking will be reconciled automatically."
                    ),
                )
                self.add_issue_labels(
                    self.admin_repository_name,
                    number,
                    ["asr-processed"],
                )
                self.close_issue(self.admin_repository_name, number)
            except PermissionError as exc:
                self.comment_issue(
                    self.admin_repository_name,
                    number,
                    f"❌ Request rejected: {exc}",
                )
                self.add_issue_labels(
                    self.admin_repository_name,
                    number,
                    ["asr-rejected"],
                )
                self.close_issue(self.admin_repository_name, number)
            except (ValueError, GitHubError) as exc:
                self.comment_issue(
                    self.admin_repository_name,
                    number,
                    f"⚠️ Automation could not process this request: `{exc}`",
                )
                self.add_issue_labels(
                    self.admin_repository_name,
                    number,
                    ["asr-automation-error"],
                )
        return created_count

    def activate_announcement_requests(self) -> None:
        self.log("Validate announcement requests")
        issues = self.list_issues(
            self.admin_repository_name,
            labels=["asr-announcement-request"],
        )
        editors = self.config["announcement_editor_teams"]
        for issue in issues:
            labels = self.issue_labels(issue)
            if labels & {
                "asr-announcement-active",
                "asr-rejected",
            }:
                continue
            number = issue["number"]
            actor = issue["user"]["login"]
            form = parse_issue_form(issue.get("body"))
            try:
                if not self.actor_in_any_team(actor, editors):
                    raise PermissionError(
                        "Announcements may only be published by release-admins, "
                        "ASR Leadership or ASR Researchers."
                    )
                scope = compact_whitespace(form.get("Scope", ""))
                if not (
                    scope.startswith("General")
                    or scope.startswith("Specific")
                ):
                    raise ValueError(
                        "Scope must be General or Specific."
                    )
                if scope.startswith("Specific"):
                    targets = split_repository_targets(
                        form.get("Target repositories", "")
                    )
                    if not targets:
                        raise ValueError(
                            "At least one target repository is required for "
                            "a specific announcement."
                        )
                    missing = [
                        name for name in targets if self.get_repository(name) is None
                    ]
                    if missing:
                        raise ValueError(
                            "Unknown target repositories: " + ", ".join(missing)
                        )
                title = compact_whitespace(
                    form.get("Announcement title", "")
                )
                message = form.get("Announcement message", "").strip()
                if not title or not message:
                    raise ValueError(
                        "Announcement title and message are required."
                    )
                expiry_raw = form.get("Expiry date (optional)", "")
                if expiry_raw and not parse_iso_date(expiry_raw):
                    raise ValueError(
                        "Expiry date must use YYYY-MM-DD."
                    )
                self.add_issue_labels(
                    self.admin_repository_name,
                    number,
                    ["asr-announcement-active"],
                )
                self.comment_issue(
                    self.admin_repository_name,
                    number,
                    (
                        "✅ Announcement approved. The repository digests are "
                        f"reconciled every {self.config.get('schedule_minutes', 15)} "
                        "minutes. Close this issue to withdraw the announcement."
                    ),
                )
            except PermissionError as exc:
                self.comment_issue(
                    self.admin_repository_name,
                    number,
                    f"❌ Announcement rejected: {exc}",
                )
                self.add_issue_labels(
                    self.admin_repository_name,
                    number,
                    ["asr-rejected"],
                )
                self.close_issue(self.admin_repository_name, number)
            except ValueError as exc:
                self.comment_issue(
                    self.admin_repository_name,
                    number,
                    f"⚠️ Announcement validation failed: {exc}",
                )
                self.add_issue_labels(
                    self.admin_repository_name,
                    number,
                    ["asr-automation-error"],
                )

    def active_announcements(self) -> list[dict[str, Any]]:
        today = utc_now().date()
        announcements: list[dict[str, Any]] = []
        for issue in self.list_issues(
            self.admin_repository_name,
            labels=["asr-announcement-active"],
        ):
            form = parse_issue_form(issue.get("body"))
            expiry = parse_iso_date(form.get("Expiry date (optional)", ""))
            if expiry and expiry < today:
                continue
            scope = compact_whitespace(form.get("Scope", ""))
            targets = (
                split_repository_targets(form.get("Target repositories", ""))
                if scope.startswith("Specific")
                else []
            )
            announcements.append(
                {
                    "number": issue["number"],
                    "url": issue["html_url"],
                    "title": compact_whitespace(
                        form.get("Announcement title", issue["title"])
                    ),
                    "message": form.get("Announcement message", "").strip(),
                    "scope": "specific" if scope.startswith("Specific") else "general",
                    "targets": targets,
                    "expiry": expiry.isoformat() if expiry else None,
                    "author": issue["user"]["login"],
                }
            )
        return announcements

    def build_announcement_digest(
        self,
        repository_name: str,
        announcements: list[dict[str, Any]],
    ) -> str:
        relevant = [
            item
            for item in announcements
            if item["scope"] == "general"
            or repository_name in item["targets"]
        ]
        general = [item for item in relevant if item["scope"] == "general"]
        specific = [item for item in relevant if item["scope"] == "specific"]

        def section(title: str, items: list[dict[str, Any]]) -> str:
            if not items:
                return f"## {title}\n\n_No active announcements._\n"
            blocks = []
            for item in items:
                expiry = (
                    f" · expires `{item['expiry']}`" if item["expiry"] else ""
                )
                blocks.append(
                    f"### [{item['title']}]({item['url']})\n\n"
                    f"{item['message']}\n\n"
                    f"_Published by `@{item['author']}`{expiry}_"
                )
            return f"## {title}\n\n" + "\n\n---\n\n".join(blocks) + "\n"

        return (
            f"{ANNOUNCEMENT_MARKER}\n"
            f"# Announcements for `{self.organization}/{repository_name}`\n\n"
            "> This issue is generated by `asr-admin-console`. Do not edit it "
            "manually; changes are replaced during reconciliation.\n\n"
            + section("Organization-wide", general)
            + "\n"
            + section("Repository-specific", specific)
            + "\n"
            "This digest changes only when an active source announcement changes.\n"
        )

    def upsert_announcement_digest(
        self,
        repository_name: str,
        body: str,
    ) -> None:
        self.ensure_labels(repository_name)
        candidates = self.list_issues(
            repository_name,
            state="all",
            labels=["asr-announcements"],
        )
        existing = next(
            (
                issue
                for issue in candidates
                if issue.get("title") == ANNOUNCEMENT_DIGEST_TITLE
            ),
            None,
        )
        if existing is None:
            self.mutate_rest(
                self.admin,
                "POST",
                f"/repos/{self.organization}/{repository_name}/issues",
                {
                    "title": ANNOUNCEMENT_DIGEST_TITLE,
                    "body": body,
                    "labels": ["asr-announcements"],
                },
            )
            return
        if existing.get("body") == body and existing.get("state") == "open":
            return
        self.mutate_rest(
            self.admin,
            "PATCH",
            (
                f"/repos/{self.organization}/{repository_name}/issues/"
                f"{existing['number']}"
            ),
            {"title": ANNOUNCEMENT_DIGEST_TITLE, "body": body, "state": "open"},
        )

    def reconcile_announcements(self) -> None:
        self.activate_announcement_requests()
        announcements = self.active_announcements()
        self.log(
            f"Reconcile announcement digests: {len(announcements)} active source(s)"
        )
        for repository in self.list_repositories(refresh=True):
            name = repository["name"]
            body = self.build_announcement_digest(name, announcements)
            try:
                self.upsert_announcement_digest(name, body)
            except GitHubError as exc:
                self.log(f"  warning: {name} digest failed: {exc}")

    def ensure_access_setup_issue(self, project_url: str) -> None:
        existing = self.list_issues(
            self.admin_repository_name,
            state="all",
            labels=["asr-access-setup"],
        )
        if any(issue["title"] == ACCESS_SETUP_TITLE for issue in existing):
            return
        body = f"""\
GitHub's public Projects API can create and update the project, fields, items,
repository links and team links, but it does not expose collaborator-role
management. Complete this one-time GitHub UI configuration:

1. Open [{self.config['incoming_project']['title']}]({project_url}).
2. Open **Settings → Manage access**.
3. Set the organization **Base role** to **Read**.
4. Grant **Write** to:
   - `@{self.organization}/release-admins`
   - `@{self.organization}/asr-leadership`
   - `@{self.organization}/asr-researchers`
5. Remove direct Write/Admin access for other teams or members.
6. Close this issue after verification.

The automation keeps the generated dashboard and calculated fields authoritative.
"""
        self.mutate_rest(
            self.admin,
            "POST",
            f"/repos/{self.admin_repository_full_name}/issues",
            {
                "title": ACCESS_SETUP_TITLE,
                "body": body,
                "labels": ["asr-access-setup"],
            },
        )

    def run(self) -> int:
        self.log(
            f"Organization={self.organization} dry_run={self.dry_run}"
        )
        repository = self.ensure_admin_repository()
        self.ensure_admin_repository_access()
        self.ensure_labels(self.admin_repository_name)

        project, project_url, _ = self.ensure_incoming_project()
        self.ensure_template_files(repository, project_url=project_url)
        self.ensure_access_setup_issue(project_url)

        created = self.process_repository_requests()
        if created:
            self.log(f"Created {created} repository/repositories; refresh project.")
            project, project_url, _ = self.ensure_incoming_project()

        self.reconcile_announcements()
        self.log(
            f"Completed reconciliation. Incoming project: {project_url}"
        )
        return 0


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GitHubError(f"Configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GitHubError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GitHubError(f"Configuration root must be an object: {path}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["reconcile"],
        help="Operation to perform.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "reconcile":
        return 2

    admin_token = os.getenv("ASR_ORG_ADMIN_TOKEN", "").strip()
    project_token = os.getenv("ASR_PROJECT_TOKEN", "").strip()
    if not admin_token:
        print("ERROR: ASR_ORG_ADMIN_TOKEN is not configured.", file=sys.stderr)
        return 2
    if not project_token:
        print("ERROR: ASR_PROJECT_TOKEN is not configured.", file=sys.stderr)
        return 2

    config_path = Path(
        os.getenv("ASR_ADMIN_CONFIG", "config/asr-admin-console.json")
    )
    access_path = Path(
        os.getenv(
            "ASR_REPOSITORY_ACCESS_CONFIG",
            "config/repository-access.json",
        )
    )
    template_root = Path(
        os.getenv("ASR_ADMIN_TEMPLATE_ROOT", "admin-console-template")
    )
    dry_run = env_bool("ASR_DRY_RUN", default=False)

    reconciler = Reconciler(
        load_json(config_path),
        load_json(access_path),
        template_root,
        admin_token=admin_token,
        project_token=project_token,
        dry_run=dry_run,
    )
    try:
        return reconciler.run()
    except GitHubError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
