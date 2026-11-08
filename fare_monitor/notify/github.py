"""Post messages as comments on the watch's GitHub issue. GitHub then emails the issue author
(and anyone subscribed) and notifies the GitHub mobile app. No extra accounts or secrets needed."""
from __future__ import annotations


class GitHubNotifier:
    name = "github"

    def __init__(self, config, session, github=None):
        self.github = github

    @classmethod
    def is_configured(cls, config, github=None) -> bool:
        return github is not None

    def send(self, message, watch=None):
        if watch is None or not watch.issue_number:
            return False
        self.github.comment(watch.issue_number, message.body)
        return True
