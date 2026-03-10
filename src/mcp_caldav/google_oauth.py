"""Google OAuth 2.0 helper for CalDAV authentication."""

import json
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger("mcp-caldav")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

DEFAULT_CLIENT_ID = "841100830294-opplvurbhn457ll94uo8i6oogb9lagce.apps.googleusercontent.com"
DEFAULT_CLIENT_SECRET = "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"

DEFAULT_TOKEN_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "mcp-caldav", "google_token.json"
)


def _build_client_config(client_id: str, client_secret: str) -> dict[str, Any]:
    """Build OAuth client config dict from ID and secret."""
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _load_token(token_path: str) -> Credentials | None:
    """Load saved credentials from token file."""
    if not os.path.exists(token_path):
        return None
    try:
        return Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception as e:
        logger.warning(f"Failed to load token from {token_path}: {e}")
        return None


def _save_token(creds: Credentials, token_path: str) -> None:
    """Save credentials to token file."""
    Path(token_path).parent.mkdir(parents=True, exist_ok=True)
    with open(token_path, "w") as f:
        f.write(creds.to_json())
    logger.info(f"Token saved to {token_path}")


def get_google_access_token(
    client_id: str | None = None,
    client_secret: str | None = None,
    client_secrets_file: str | None = None,
    token_path: str | None = None,
    force_new: bool = False,
) -> str:
    """
    Get a valid Google OAuth access token, refreshing or running the
    authorization flow as needed.

    Provide either (client_id + client_secret) or client_secrets_file.

    Args:
        client_id: Google OAuth client ID
        client_secret: Google OAuth client secret
        client_secrets_file: Path to Google client_secrets.json
        token_path: Path to store/load the token (default: ~/.config/mcp-caldav/google_token.json)

    Returns:
        A valid access token string
    """
    token_path = token_path or DEFAULT_TOKEN_PATH

    if not force_new:
        creds = _load_token(token_path)

        if creds and creds.valid:
            return creds.token

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                _save_token(creds, token_path)
                return creds.token
            except Exception as e:
                logger.warning(f"Token refresh failed, re-authorizing: {e}")

    # Need to run the OAuth flow
    client_id = client_id or DEFAULT_CLIENT_ID
    client_secret = client_secret or DEFAULT_CLIENT_SECRET

    if client_secrets_file and os.path.exists(client_secrets_file):
        flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
    else:
        client_config = _build_client_config(client_id, client_secret)
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)

    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds, token_path)
    return creds.token


def fetch_google_calendar_list(access_token: str, user_email: str) -> list[dict[str, Any]]:
    """Fetch all calendars via the Google Calendar REST API.

    This returns the complete list of calendars visible to the user — including
    subscribed calendars, shared calendars from other people, and team calendars —
    which Google's CalDAV principal endpoint does not expose.

    Args:
        access_token: A valid Google OAuth access token.
        user_email: The authenticated user's email address (used to construct CalDAV URLs).

    Returns:
        List of calendar dicts with keys: id, name, caldav_url, access_role, primary.
    """
    encoded_email = urllib.parse.quote(user_email, safe="")
    base_caldav = f"https://apidata.googleusercontent.com/caldav/v2/{encoded_email}"

    url = "https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=250"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())

    calendars = []
    for item in data.get("items", []):
        cal_id: str = item["id"]
        name: str = item.get("summary", cal_id)
        primary: bool = item.get("primary", False)
        access_role: str = item.get("accessRole", "")

        if primary:
            caldav_url = f"{base_caldav}/events/"
        else:
            caldav_url = f"{base_caldav}/{urllib.parse.quote(cal_id, safe='')}/"

        calendars.append(
            {
                "id": cal_id,
                "name": name,
                "caldav_url": caldav_url,
                "access_role": access_role,
                "primary": primary,
            }
        )

    logger.debug(f"Fetched {len(calendars)} calendars for {user_email}")
    return calendars
