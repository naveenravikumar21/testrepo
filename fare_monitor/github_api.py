"""Minimal GitHub REST client for watch issues and their comments (uses the Actions GITHUB_TOKEN)."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)
API = "https://api.github.com"


class GitHubError(Exception):
    pass


class GitHubClient:
    def __init__(self, token: str, repository: str, session):
        self.repo = repository
        self.session = session
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _req(self, method: str, path: str, **kw):
        fn = getattr(self.session, method.lower())
        resp = fn(f"{API}{path}", headers=self.headers, timeout=30, **kw)
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub {method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.text else {}

    def open_issues(self) -> list[dict]:
        issues, page = [], 1
        while True:
            batch = self._req("GET", f"/repos/{self.repo}/issues",
                              params={"state": "open", "per_page": 100, "page": page})
            issues += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                return issues
            page += 1

    def get_issue(self, number: int) -> dict:
        return self._req("GET", f"/repos/{self.repo}/issues/{number}")

    def comment(self, number: int, body: str) -> int:
        return self._req("POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})["id"]

    def update_comment(self, comment_id: int, body: str):
        self._req("PATCH", f"/repos/{self.repo}/issues/comments/{comment_id}", json={"body": body})

    def close_issue(self, number: int):
        self._req("PATCH", f"/repos/{self.repo}/issues/{number}", json={"state": "closed", "state_reason": "completed"})

    def ensure_label(self, name: str, color: str = "0e8a16", description: str = "Flight fare watch"):
        try:
            self._req("POST", f"/repos/{self.repo}/labels", json={"name": name, "color": color,
                                                                  "description": description})
        except GitHubError as exc:
            if "422" not in str(exc):  # 422 = already exists
                log.warning("%s", exc)

    def add_label(self, number: int, name: str):
        try:
            self._req("POST", f"/repos/{self.repo}/issues/{number}/labels", json={"labels": [name]})
        except GitHubError as exc:
            log.warning("%s", exc)
