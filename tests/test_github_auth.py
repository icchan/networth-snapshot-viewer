"""Offline tests for GitHub personal access token validation."""

import requests
from helpers import check

import github_auth


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


def test_username_lookup_uses_token_and_returns_login(monkeypatch):
    seen = {}

    def get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return FakeResponse({"login": "octocat"})

    monkeypatch.setattr(github_auth.requests, "get", get)
    username = github_auth._github_username("personal-access-token")
    check("GitHub username is returned", username == "octocat")
    check(
        "account lookup authenticates with the entered token",
        seen["headers"]["Authorization"] == "Bearer personal-access-token",
    )


def test_invalid_token_has_clear_message(monkeypatch):
    monkeypatch.setattr(
        github_auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"message": "Bad credentials"}, status_code=401),
    )
    try:
        github_auth._github_username("invalid-token")
    except github_auth.GitHubAuthError as exc:
        check("invalid token is identified", "rejected" in str(exc))
    else:
        check("invalid token raises an authentication error", False)


def test_rate_limit_has_clear_message(monkeypatch):
    monkeypatch.setattr(
        github_auth.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {"message": "API rate limit exceeded"},
            status_code=403,
            headers={"X-RateLimit-Remaining": "0"},
        ),
    )
    try:
        github_auth._github_username("valid-token")
    except github_auth.GitHubAuthError as exc:
        check("rate limit is identified", "rate limit" in str(exc).lower())
    else:
        check("rate-limited lookup raises an authentication error", False)


def test_browser_storage_call_is_scoped_and_actionable(monkeypatch):
    seen = {}

    def component(**kwargs):
        seen.update(kwargs)
        return {"ready": True}

    monkeypatch.setattr(github_auth, "_browser_pat_component", component)
    result = github_auth._browser_pat_storage("save", "operation-1", token="secret")
    check("browser operation result is returned", result == {"ready": True})
    check(
        "token storage uses the app's fixed localStorage key",
        seen["storage_key"] == github_auth.BROWSER_PAT_STORAGE_KEY,
    )
    check("save action carries the token only to the browser component", seen["token"] == "secret")
    check(
        "operation IDs let the app reject stale component replies",
        seen["operation_id"] == "operation-1",
    )
