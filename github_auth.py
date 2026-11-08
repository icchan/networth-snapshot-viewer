"""User-provided GitHub token login for the Streamlit page."""

from __future__ import annotations

import uuid
from pathlib import Path

import requests
import streamlit as st
from streamlit.components.v1 import declare_component

GITHUB_API_URL = "https://api.github.com"
BROWSER_PAT_STORAGE_KEY = "finance-tracker.github-pat.v1"
_browser_pat_component = declare_component(
    "browser_pat_storage", path=Path(__file__).parent / "browser_pat_storage"
)


class GitHubAuthError(RuntimeError):
    """GitHub could not authenticate the supplied token."""


def _browser_pat_storage(
    action: str,
    operation_id: str,
    *,
    token: str | None = None,
    default: dict | None = None,
) -> dict:
    """Run one browser-local PAT storage operation through a same-origin component."""
    return _browser_pat_component(
        action=action,
        operation_id=operation_id,
        storage_key=BROWSER_PAT_STORAGE_KEY,
        token=token,
        default=default or {"ready": False},
        key="github_browser_pat_storage",
        tab_index=-1,
    )


def _storage_action(action: str, token: str | None = None) -> dict[str, str]:
    return {"action": action, "operation_id": uuid.uuid4().hex, "token": token or ""}


def _github_username(token: str) -> str:
    try:
        response = requests.get(
            f"{GITHUB_API_URL}/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise GitHubAuthError("Could not connect to GitHub. Please try again.") from exc

    if response.status_code == 401:
        raise GitHubAuthError("GitHub rejected that token. Check it and try again.")
    if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
        raise GitHubAuthError("GitHub API rate limit reached. Please try again later.")
    try:
        response.raise_for_status()
        username = response.json().get("login")
    except requests.RequestException as exc:
        raise GitHubAuthError(
            f"GitHub account lookup failed (HTTP {response.status_code})."
        ) from exc
    except (ValueError, AttributeError) as exc:
        raise GitHubAuthError("GitHub returned an invalid account response.") from exc
    if not isinstance(username, str) or not username:
        raise GitHubAuthError("GitHub did not return an account name.")
    return username


def require_github_login() -> tuple[str, str]:
    """Return the session token and username, or show token login and stop.

    Remembered tokens are opt-in and live in this browser's localStorage, not
    in the URL or portfolio data.
    """
    token = st.session_state.get("github_access_token")
    username = st.session_state.get("github_username")

    pending_action = st.session_state.get("_browser_pat_action")
    storage_component_rendered = False
    if pending_action:
        storage_component_rendered = True
        result = _browser_pat_storage(
            pending_action["action"],
            pending_action["operation_id"],
            token=pending_action["token"],
        )
        if (
            isinstance(result, dict)
            and result.get("ready")
            and result.get("operation_id") == pending_action["operation_id"]
        ):
            st.session_state.pop("_browser_pat_action")
            if result.get("error"):
                st.error(f"Browser token storage failed: {result['error']}")

    if isinstance(token, str) and token and isinstance(username, str) and username:
        st.sidebar.caption(f"Signed in to GitHub as @{username}")
        if st.sidebar.button("Log out", key="github_logout"):
            st.session_state.pop("github_access_token", None)
            st.session_state.pop("github_username", None)
            st.session_state["_browser_pat_action"] = _storage_action("clear")
            st.rerun()
        return token, username

    if not storage_component_rendered:
        load_id = st.session_state.setdefault("_browser_pat_load_id", uuid.uuid4().hex)
        stored = _browser_pat_storage(
            "load",
            load_id,
            default={"ready": False},
        )
        saved_token = (
            stored.get("token")
            if isinstance(stored, dict)
            and stored.get("ready")
            and stored.get("operation_id") == load_id
            else None
        )
        if isinstance(stored, dict) and stored.get("error"):
            st.warning(f"Could not access saved browser login: {stored['error']}")
        if isinstance(saved_token, str) and saved_token:
            try:
                username = _github_username(saved_token)
            except GitHubAuthError as exc:
                st.error(str(exc))
            else:
                st.session_state["github_access_token"] = saved_token
                st.session_state["github_username"] = username
                st.rerun()

    st.title("Portfolio")
    st.write("Enter a GitHub personal access token to use your private portfolio repository.")
    st.caption(
        "Create a classic personal access token with the `repo` scope. The token is "
        "validated by GitHub and kept only in this Streamlit session unless you opt "
        "to remember it in this browser."
    )
    with st.form("github_token_login"):
        entered_token = st.text_input("GitHub personal access token", type="password")
        remember = st.checkbox(
            "Remember this browser",
            help=(
                "Stores the PAT in this browser's localStorage so it survives refreshes. "
                "Only use this on a trusted device; scripts running on this app's origin "
                "can access it."
            ),
        )
        submitted = st.form_submit_button("Log in with GitHub", type="primary")

    if submitted:
        if not entered_token.strip():
            st.error("Enter a GitHub personal access token.")
            st.stop()
        try:
            username = _github_username(entered_token.strip())
        except GitHubAuthError as exc:
            st.error(str(exc))
            st.stop()
        entered_token = entered_token.strip()
        st.session_state["github_access_token"] = entered_token
        st.session_state["github_username"] = username
        st.session_state["_browser_pat_action"] = _storage_action(
            "save" if remember else "clear",
            entered_token if remember else None,
        )
        st.rerun()
        st.stop()

    st.stop()
