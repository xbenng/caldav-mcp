"""Google OAuth 2.0 helper for CalDAV authentication."""

import json
import logging
import os
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
