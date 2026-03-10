"""Google OAuth 2.0 helper and Google Calendar REST API client."""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import EventRecord

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


_RESPONSE_STATUS_MAP = {
    "accepted": "A",
    "declined": "D",
    "tentative": "T",
    "needsAction": "N",
}


def _parse_google_event(item: dict[str, Any]) -> "EventRecord | None":
    """Convert a Google Calendar REST API event item to an EventRecord."""
    start_info = item.get("start", {})
    end_info = item.get("end", {})

    if "dateTime" in start_info:
        start_str = start_info["dateTime"]
        all_day = False
    elif "date" in start_info:
        start_str = start_info["date"] + "T00:00:00"
        all_day = True
    else:
        return None

    if "dateTime" in end_info:
        end_str = end_info["dateTime"]
    elif "date" in end_info:
        end_str = end_info["date"] + "T23:59:59"
    else:
        end_str = start_str

    attendees = [
        {
            "email": a.get("email", ""),
            "status": _RESPONSE_STATUS_MAP.get(a.get("responseStatus", ""), "N"),
        }
        for a in item.get("attendees", [])
    ]

    rrules = item.get("recurrence", [])
    recurrence = "\n".join(r for r in rrules if r.startswith("RRULE")) or None

    return {
        "uid": item.get("id", ""),
        "title": item.get("summary", ""),
        "start": start_str,
        "end": end_str,
        "description": item.get("description", ""),
        "location": item.get("location", ""),
        "all_day": all_day,
        "categories": [],
        "priority": None,
        "recurrence": recurrence,
        "attendees": attendees,
    }


class GoogleCalendarAPI:
    """Google Calendar REST API client for event operations.

    Used as the primary event backend for Google OAuth accounts, enabling
    access to all calendars (including read-only subscribed/shared ones)
    that the CalDAV endpoint cannot reach.
    """

    BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _get(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())  # type: ignore[no-any-return]

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={**self._headers(), "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())  # type: ignore[no-any-return]

    def _delete(self, url: str) -> None:
        req = urllib.request.Request(url, headers=self._headers(), method="DELETE")
        with urllib.request.urlopen(req):
            pass

    def get_events(
        self,
        calendar_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        include_all_day: bool = True,
    ) -> "list[EventRecord]":
        """Fetch events for a date range, handling pagination."""
        from datetime import timezone

        now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        time_min = (start_date or now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = (end_date or (now.replace(hour=23, minute=59, second=59))).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        params = urllib.parse.urlencode({
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "2500",
        })

        results: list[Any] = []
        page_token: str | None = None

        while True:
            page_params = params + (f"&pageToken={page_token}" if page_token else "")
            data = self._get(f"{self.BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events?{page_params}")
            results.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        events = []
        for item in results:
            if item.get("status") == "cancelled":
                continue
            parsed = _parse_google_event(item)
            if parsed is None:
                continue
            if not include_all_day and parsed["all_day"]:
                continue
            events.append(parsed)

        return events

    def get_event_by_uid(self, calendar_id: str, uid: str) -> "EventRecord | None":
        """Fetch a single event by its Google event ID."""
        try:
            item = self._get(
                f"{self.BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}"
                f"/events/{urllib.parse.quote(uid, safe='')}"
            )
            return _parse_google_event(item)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def create_event(
        self,
        calendar_id: str,
        title: str,
        description: str = "",
        location: str = "",
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        duration_hours: float = 1.0,
        attendees: list[Any] | None = None,
        recurrence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a calendar event via the Google Calendar REST API."""
        from datetime import timedelta

        if start_time is None:
            start_time = (datetime.now() + timedelta(days=1)).replace(
                hour=14, minute=0, second=0, microsecond=0
            )
        if end_time is None:
            end_time = start_time + timedelta(hours=duration_hours)

        body: dict[str, Any] = {
            "summary": title,
            "description": description,
            "location": location,
            "start": {"dateTime": start_time.isoformat()},
            "end": {"dateTime": end_time.isoformat()},
        }

        if attendees:
            body["attendees"] = [
                {"email": (a if isinstance(a, str) else a.get("email", ""))}
                for a in attendees
                if (a if isinstance(a, str) else a.get("email", ""))
            ]

        if recurrence:
            frequency = recurrence.get("frequency", "DAILY").upper()
            rrule = f"RRULE:FREQ={frequency}"
            if recurrence.get("interval", 1) > 1:
                rrule += f";INTERVAL={recurrence['interval']}"
            if recurrence.get("count"):
                rrule += f";COUNT={recurrence['count']}"
            if recurrence.get("byday"):
                rrule += f";BYDAY={recurrence['byday']}"
            body["recurrence"] = [rrule]

        result = self._post(
            f"{self.BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events",
            body,
        )
        return {
            "success": True,
            "uid": result.get("id", ""),
            "title": title,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "calendar": calendar_id,
        }

    def delete_event(self, calendar_id: str, uid: str) -> bool:
        """Delete an event by its Google event ID."""
        try:
            self._delete(
                f"{self.BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}"
                f"/events/{urllib.parse.quote(uid, safe='')}"
            )
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise

    def search_events(
        self,
        calendar_id: str,
        query: str | None,
        search_fields: list[str] | None,
        start_date: datetime | None,
        end_date: datetime | None,
    ) -> "list[EventRecord]":
        """Search events. Uses Google's full-text search when querying title/description,
        otherwise fetches all events in the range and filters client-side."""
        if query and (search_fields is None or "title" in search_fields or "description" in search_fields):
            # Use Google's server-side search (searches title+description)
            from datetime import timezone

            now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            time_min = (start_date or now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            time_max = (end_date or (now.replace(hour=23, minute=59, second=59))).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            params = urllib.parse.urlencode({
                "q": query,
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": "2500",
            })
            data = self._get(
                f"{self.BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events?{params}"
            )
            events = [_parse_google_event(i) for i in data.get("items", []) if i.get("status") != "cancelled"]
            return [e for e in events if e is not None]

        # Client-side filter for location/attendees
        all_events = self.get_events(calendar_id, start_date, end_date)
        if not query:
            return all_events

        needle = query.lower()
        fields = search_fields or ["title", "description", "location", "attendees"]
        results = []
        for event in all_events:
            match = (
                ("location" in fields and needle in event.get("location", "").lower())
                or ("attendees" in fields and any(
                    needle in a.get("email", "").lower()
                    for a in (event.get("attendees") or [])
                ))
            )
            if match:
                results.append(event)
        return results
