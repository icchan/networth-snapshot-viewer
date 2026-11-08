"""Portfolio persistence in each user's dedicated private GitHub repository."""

from __future__ import annotations

import base64
from copy import deepcopy
from urllib.parse import quote

import requests
import yaml

from snapshots import PortfolioError, check_handle

GITHUB_API_URL = "https://api.github.com"
REPOSITORY_NAME = "streamlit-portfolio-data"
PORTFOLIO_DIRECTORY = "portfolios"
DEFAULT_PORTFOLIO = {
    "currencies": ["USD"],
    "assets": [{"key": "cash", "name": "Cash", "type": "cash", "currency": "USD"}],
    "snapshots": [],
}


class GitHubStorageError(RuntimeError):
    """A GitHub API or portfolio persistence operation failed."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _response_json(response: requests.Response) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubStorageError("GitHub returned an invalid JSON response.") from exc


def _request(
    method: str,
    url: str,
    user_token: str,
    **kwargs,
) -> requests.Response:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {user_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if "headers" in kwargs:
        headers.update(kwargs.pop("headers"))
    try:
        response = requests.request(method, url, headers=headers, timeout=20, **kwargs)
    except requests.RequestException as exc:
        raise GitHubStorageError(f"GitHub request failed: {exc}") from exc

    if not response.ok:
        message = f"GitHub API request failed with HTTP {response.status_code}"
        try:
            detail = response.json().get("message")
        except (ValueError, AttributeError):
            detail = None
        if response.status_code == 401:
            message = "GitHub rejected the access token. Please log in again."
        elif response.status_code == 403 and (
            response.headers.get("X-RateLimit-Remaining") == "0"
            or (isinstance(detail, str) and "rate limit" in detail.lower())
        ):
            message = "GitHub API rate limit reached. Please try again later."
        elif isinstance(detail, str):
            message = f"{message}: {detail}"
        raise GitHubStorageError(message, status_code=response.status_code)
    return response


def _repository_url(username: str) -> str:
    owner = quote(username, safe="")
    repository = quote(REPOSITORY_NAME, safe="")
    return f"{GITHUB_API_URL}/repos/{owner}/{repository}"


def _get_repository(user_token: str, username: str) -> dict | None:
    try:
        response = _request("GET", _repository_url(username), user_token)
    except GitHubStorageError as exc:
        if exc.status_code == 404:
            return None
        raise
    repository = _response_json(response)
    if not isinstance(repository, dict):
        raise GitHubStorageError("GitHub returned an invalid repository response.")
    return repository


def _default_branch(repository: dict) -> str:
    branch = repository.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise GitHubStorageError(f"The {REPOSITORY_NAME} repository has no default branch.")
    return branch


def _username_for_token(user_token: str, username: str | None) -> str:
    if username:
        return username
    response = _request("GET", f"{GITHUB_API_URL}/user", user_token)
    account = _response_json(response)
    if not isinstance(account, dict):
        raise GitHubStorageError("GitHub returned an invalid account response.")
    login = account.get("login")
    if not isinstance(login, str) or not login:
        raise GitHubStorageError("GitHub did not return an account name.")
    return login


def list_portfolios(user_token: str, username: str) -> list[str]:
    """List valid portfolio handles stored in the user's private repository."""
    repository = _get_repository(user_token, username)
    if repository is None:
        return []

    url = f"{_repository_url(username)}/contents/{PORTFOLIO_DIRECTORY}"
    try:
        response = _request("GET", url, user_token, params={"ref": _default_branch(repository)})
    except GitHubStorageError as exc:
        if exc.status_code == 404:
            return []
        raise
    entries = _response_json(response)
    if not isinstance(entries, list):
        raise GitHubStorageError("The portfolio directory is not a GitHub file listing.")

    handles = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.endswith(".yaml"):
            continue
        try:
            handles.append(check_handle(name[:-5]))
        except PortfolioError:
            continue
    return sorted(set(handles))


def _file_url(username: str, handle: str) -> str:
    filename = quote(f"{PORTFOLIO_DIRECTORY}/{handle}.yaml", safe="/")
    return f"{_repository_url(username)}/contents/{filename}"


def load_portfolio(
    user_token: str,
    portfolio_name: str,
    username: str | None = None,
) -> dict:
    """Load a portfolio mapping, or return an empty starter portfolio."""
    handle = check_handle(portfolio_name)
    username = _username_for_token(user_token, username)
    repository = _get_repository(user_token, username)
    if repository is None:
        return deepcopy(DEFAULT_PORTFOLIO)

    try:
        response = _request(
            "GET",
            _file_url(username, handle),
            user_token,
            params={"ref": _default_branch(repository)},
        )
    except GitHubStorageError as exc:
        if exc.status_code == 404:
            return deepcopy(DEFAULT_PORTFOLIO)
        raise

    try:
        response_data = _response_json(response)
        if not isinstance(response_data, dict):
            raise TypeError("GitHub did not return a file response.")
        encoded = response_data["content"]
        if not isinstance(encoded, str):
            raise TypeError("GitHub did not return encoded file content.")
        content = base64.b64decode("".join(encoded.split()), validate=True).decode("utf-8")
        portfolio = yaml.safe_load(content)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise GitHubStorageError(
            f"{handle}.yaml in GitHub is not valid YAML content: {exc}"
        ) from exc
    if not isinstance(portfolio, dict):
        raise GitHubStorageError(f"{handle}.yaml in GitHub must contain a YAML mapping.")
    return portfolio


def _ensure_repository(user_token: str, username: str) -> dict:
    repository = _get_repository(user_token, username)
    if repository is not None:
        return repository
    response = _request(
        "POST",
        f"{GITHUB_API_URL}/user/repos",
        user_token,
        json={
            "name": REPOSITORY_NAME,
            "description": "Private storage for Streamlit portfolio data",
            "private": True,
            "auto_init": True,
        },
    )
    repository = _response_json(response)
    if not isinstance(repository, dict):
        raise GitHubStorageError("GitHub returned an invalid repository response.")
    return repository


def save_portfolio(
    user_token: str,
    portfolio_name: str,
    portfolio_data: dict,
    username: str | None = None,
) -> None:
    """Create or update one portfolio YAML file in the user's private repository."""
    handle = check_handle(portfolio_name)
    username = _username_for_token(user_token, username)
    repository = _ensure_repository(user_token, username)
    url = _file_url(username, handle)
    branch = _default_branch(repository)

    try:
        current = _response_json(_request("GET", url, user_token, params={"ref": branch}))
    except GitHubStorageError as exc:
        if exc.status_code != 404:
            raise
        current = None
    if current is not None and not isinstance(current, dict):
        raise GitHubStorageError(f"GitHub returned an invalid file response for {handle}.yaml.")

    try:
        content = yaml.safe_dump(
            portfolio_data,
            sort_keys=False,
            allow_unicode=True,
        ).encode("utf-8")
    except yaml.YAMLError as exc:
        raise GitHubStorageError(f"Could not serialize {handle}.yaml: {exc}") from exc
    payload = {
        "message": f"Save portfolio {handle}",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if isinstance(current, dict) and isinstance(current.get("sha"), str):
        payload["sha"] = current["sha"]
    _request("PUT", url, user_token, json=payload)
