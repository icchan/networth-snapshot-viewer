"""Offline tests for private-repository portfolio persistence."""

import base64

import pytest
import yaml
from helpers import check

import github_storage
from github_storage import GitHubStorageError


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self.payload


def test_list_portfolios_filters_and_sorts_yaml_handles(monkeypatch):
    def request(method, url, **kwargs):
        if url.endswith("/streamlit-portfolio-data"):
            return FakeResponse(200, {"default_branch": "main"})
        return FakeResponse(
            200,
            [
                {"name": "family.yaml", "type": "file"},
                {"name": "README.md", "type": "file"},
                {"name": "../unsafe.yaml", "type": "file"},
                {"name": "personal.yaml", "type": "file"},
                {"name": "folder", "type": "dir"},
            ],
        )

    monkeypatch.setattr(github_storage.requests, "request", request)
    handles = github_storage.list_portfolios("token", "owner")
    check("only valid yaml portfolio handles are listed", handles == ["family", "personal"])


def test_load_portfolio_parses_yaml_and_returns_default_when_missing(monkeypatch):
    yaml_content = yaml.safe_dump({"currencies": ["AUD"], "assets": [], "snapshots": []})
    encoded = base64.b64encode(yaml_content.encode()).decode()

    def request(method, url, **kwargs):
        if url.endswith("/user"):
            return FakeResponse(200, {"login": "owner"})
        if url.endswith("/streamlit-portfolio-data"):
            return FakeResponse(200, {"default_branch": "main"})
        if url.endswith("/personal.yaml"):
            return FakeResponse(200, {"content": encoded})
        if url.endswith("/new.yaml"):
            return FakeResponse(404, {"message": "Not Found"})
        raise AssertionError(f"unexpected API call: {method} {url}")

    monkeypatch.setattr(github_storage.requests, "request", request)
    loaded = github_storage.load_portfolio("token", "personal")
    starter = github_storage.load_portfolio("token", "new", username="owner")
    check("YAML document is parsed as a mapping", loaded["currencies"] == ["AUD"])
    check(
        "missing portfolio returns starter structure", starter == github_storage.DEFAULT_PORTFOLIO
    )
    check(
        "starter data is not shared between callers",
        starter["assets"] is not github_storage.DEFAULT_PORTFOLIO["assets"],
    )


def test_save_creates_private_repository_and_portfolio_file(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "GET" and url.endswith("/streamlit-portfolio-data"):
            return FakeResponse(404, {"message": "Not Found"})
        if method == "POST" and url.endswith("/user/repos"):
            return FakeResponse(201, {"default_branch": "main"})
        if method == "GET" and url.endswith("/personal.yaml"):
            return FakeResponse(404, {"message": "Not Found"})
        if method == "PUT" and url.endswith("/personal.yaml"):
            return FakeResponse(201, {})
        raise AssertionError(f"unexpected API call: {method} {url}")

    monkeypatch.setattr(github_storage.requests, "request", request)
    github_storage.save_portfolio(
        "token",
        "personal",
        {"currencies": ["USD"], "assets": [], "snapshots": []},
        username="owner",
    )

    creation = next(call for call in calls if call[0] == "POST")
    check("new repository is private", creation[2]["json"]["private"] is True)
    update = next(call for call in calls if call[0] == "PUT")
    saved_data = yaml.safe_load(base64.b64decode(update[2]["json"]["content"]))
    check("portfolio data is encoded as YAML content", saved_data["currencies"] == ["USD"])
    check("portfolio is stored under its handle", "/portfolios/personal.yaml" in update[1])


def test_save_updates_existing_file_with_current_sha(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/streamlit-portfolio-data"):
            return FakeResponse(200, {"default_branch": "main"})
        if method == "GET" and url.endswith("/personal.yaml"):
            return FakeResponse(200, {"sha": "current-sha"})
        if method == "PUT" and url.endswith("/personal.yaml"):
            return FakeResponse(200, {})
        raise AssertionError(f"unexpected API call: {method} {url}")

    monkeypatch.setattr(github_storage.requests, "request", request)
    github_storage.save_portfolio(
        "token",
        "personal",
        {"currencies": ["AUD"], "assets": [], "snapshots": []},
        username="owner",
    )
    update = next(call for call in calls if call[0] == "PUT")
    check("updating sends the current file SHA", update[2]["json"]["sha"] == "current-sha")


@pytest.mark.parametrize(
    ("status_code", "headers", "expected"),
    [
        (401, {}, "Please log in again"),
        (403, {"X-RateLimit-Remaining": "0"}, "rate limit"),
    ],
)
def test_api_errors_are_reported_without_hiding_failure(
    monkeypatch, status_code, headers, expected
):
    monkeypatch.setattr(
        github_storage.requests,
        "request",
        lambda *args, **kwargs: FakeResponse(status_code, {"message": "API failure"}, headers),
    )
    with pytest.raises(GitHubStorageError, match=expected):
        github_storage.list_portfolios("token", "owner")
