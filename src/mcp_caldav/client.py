"""CalDAV client for calendar operations."""

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    import caldav

try:
    import caldav
except ImportError as err:
    raise ImportError(
        "caldav library is not installed. Install it with: pip install caldav"
    ) from err


class CalendarInfo(TypedDict):
    index: int
    name: str
    url: str


class EventAttendee(TypedDict, total=False):
    email: str
    status: str
    name: str


class EventRecord(TypedDict, total=False):
    uid: str
    title: str
    start: str
    end: str
    description: str
    location: str
    all_day: bool
    categories: list[str]
    priority: int | None
    recurrence: str | None
    attendees: list[EventAttendee]


class EventCreationResult(TypedDict):
    success: bool
    uid: str
    title: str
    start_time: str
    end_time: str
    calendar: str


class EventDeletionResult(TypedDict):
    success: bool
    uid: str
    message: str


AttendeeInput = EventAttendee | str


def _escape_ical_text(value: str | Any) -> str:
    """Escape text for inclusion in iCalendar payloads."""
    if not isinstance(value, str):
        value = str(value)
    result: str = (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )
    return result


def _format_rrule(recurrence: dict) -> str:
    """
    Format recurrence rule (RRULE) from dictionary to iCalendar RRULE string.

    Args:
        recurrence: Dictionary with keys:
            - frequency: 'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'
            - interval: int (optional, default 1)
            - count: int (optional, number of occurrences)
            - until: datetime or date (optional, end date)
            - byday: str (optional, e.g., 'MO,WE,FR' for Monday, Wednesday, Friday)
            - bymonthday: int (optional, day of month)
            - bymonth: int (optional, month 1-12)

    Returns:
        RRULE string for iCalendar
    """
    if not recurrence:
        return ""

    frequency = recurrence.get("frequency", "DAILY").upper()
    if frequency not in ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]:
        raise ValueError(f"Invalid frequency: {frequency}")

    parts = [f"FREQ={frequency}"]

    interval = recurrence.get("interval", 1)
    if interval > 1:
        parts.append(f"INTERVAL={interval}")

    count = recurrence.get("count")
    if count:
        parts.append(f"COUNT={count}")

    until = recurrence.get("until")
    if until:
        if isinstance(until, datetime):
            until_str = until.strftime("%Y%m%dT%H%M%SZ")
        elif isinstance(until, date):
            until_str = until.strftime("%Y%m%d")
        else:
            until_str = str(until)
        parts.append(f"UNTIL={until_str}")

    byday = recurrence.get("byday")
    if byday:
        parts.append(f"BYDAY={byday}")

    bymonthday = recurrence.get("bymonthday")
    if bymonthday:
        parts.append(f"BYMONTHDAY={bymonthday}")

    bymonth = recurrence.get("bymonth")
    if bymonth:
        parts.append(f"BYMONTH={bymonth}")

    return f"RRULE:{';'.join(parts)}"


def _format_categories(categories: list[str]) -> str:
    """
    Format categories list to iCalendar CATEGORIES string.

    Args:
        categories: List of category strings

    Returns:
        CATEGORIES line for iCalendar
    """
    if not categories:
        return ""
    # Escape commas in categories and join with commas
    escaped = [cat.replace(",", "\\,").replace(";", "\\;") for cat in categories]
    return f"CATEGORIES:{','.join(escaped)}"


def _format_attendees(attendees: list[AttendeeInput]) -> str:
    """
    Format attendees to iCalendar ATTENDEE lines.

    Args:
        attendees: List of email strings or dicts with 'email' and optional 'status'
            Status can be: 'ACCEPTED', 'DECLINED', 'TENTATIVE', 'NEEDS-ACTION'

    Returns:
        ATTENDEE lines for iCalendar
    """
    if not attendees:
        return ""

    attendee_lines = []
    for attendee in attendees:
        display_name = ""
        if isinstance(attendee, str):
            email = attendee.strip()
            status = None
        elif isinstance(attendee, dict):
            email = attendee.get("email", "").strip()
            status = attendee.get("status", "").upper()
            display_name = attendee.get("name", email)
        else:
            # Skip invalid attendee types (should not happen with proper typing)
            continue  # type: ignore[unreachable]

        if "@" not in email:
            continue

        # Build ATTENDEE line
        cn_value = _escape_ical_text(display_name or email)
        params = ["RSVP=TRUE", f"CN={cn_value}"]
        if status and status in ["ACCEPTED", "DECLINED", "TENTATIVE", "NEEDS-ACTION"]:
            params.append(f"PARTSTAT={status}")

        attendee_line = f"ATTENDEE;{';'.join(params)}:mailto:{email}"
        attendee_lines.append(attendee_line)

    return "\n".join(attendee_lines) + "\n" if attendee_lines else ""


def _parse_categories(cats: Any) -> list[str]:
    """
    Parse categories from iCalendar component.

    Args:
        cats: CATEGORIES value from iCalendar component

    Returns:
        List of category strings
    """
    if not cats:
        return []

    categories = []
    try:
        # Handle vText objects or lists
        if hasattr(cats, "cats"):
            # Multiple categories as list
            for cat in cats.cats:
                if hasattr(cat, "value"):
                    categories.append(str(cat.value))
                else:
                    categories.append(str(cat))
        elif hasattr(cats, "value"):
            # Single category as vText
            value = cats.value
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            categories = [c.strip() for c in str(value).split(",")]
        elif isinstance(cats, list):
            # List of category objects
            for cat in cats:
                if hasattr(cat, "value"):
                    val = cat.value
                    if isinstance(val, bytes):
                        val = val.decode("utf-8")
                    categories.append(str(val))
                else:
                    categories.append(str(cat))
        else:
            # String format
            cat_str = str(cats)
            if isinstance(cats, bytes):
                cat_str = cats.decode("utf-8")
            categories = [c.strip() for c in cat_str.split(",")]
    except Exception:
        # Fallback: try to convert to string
        try:
            categories = [str(cats)]
        except Exception:
            categories = []

    return [c for c in categories if c]


def _parse_attendees(ical_component: Any) -> list[EventAttendee]:
    """
    Parse attendees from iCalendar component.

    Args:
        ical_component: iCalendar component object

    Returns:
        List of attendee dictionaries with 'email' and 'status'
    """
    attendees: list[EventAttendee] = []
    attendee_list = ical_component.get("ATTENDEE", [])
    if not isinstance(attendee_list, list):
        attendee_list = [attendee_list]

    for attendee in attendee_list:
        try:
            if hasattr(attendee, "params"):
                email = str(attendee).replace("mailto:", "")
                status = (
                    attendee.params.get("PARTSTAT", ["NEEDS-ACTION"])[0]
                    if hasattr(attendee, "params")
                    else "NEEDS-ACTION"
                )
                attendees.append({"email": email, "status": status})
            else:
                # Fallback for string format
                email = str(attendee).replace("mailto:", "").strip()
                if email:
                    attendees.append({"email": email, "status": "NEEDS-ACTION"})
        except Exception:
            continue

    return attendees


class CalDAVClient:
    """
    Client for working with CalDAV calendars.

    Note: Some CalDAV providers (like Yandex Calendar) have rate limiting.
    Yandex Calendar artificially slows down WebDAV operations (60 seconds per MB since 2021),
    which can cause frequent 504 timeouts, especially when creating/updating events.
    """

    def __init__(
        self,
        url: str,
        username: str = "",
        password: str = "",
        auth_type: str = "basic",
    ):
        """
        Initialize CalDAV client.

        Args:
            url: CalDAV server URL (e.g., "https://caldav.example.com/")
            username: Username for authentication (basic auth)
            password: Password, app password, or OAuth access token
            auth_type: "basic" for username/password, "bearer" for OAuth token
        """
        self.url = url
        self.username = username
        self.password = password
        self.auth_type = auth_type
        self.client: Any | None = None
        self.principal: Any | None = None
        # Full calendar list from Google Calendar REST API (includes subscribed/shared calendars).
        # When set, list_calendars() and _resolve_calendar() use this instead of principal.calendars().
        self._google_calendars: list[dict] | None = None
        self._google_api: Any | None = None  # GoogleCalendarAPI instance, set via set_google_calendars
        # Detect Yandex Calendar for special handling
        self.is_yandex = "yandex.ru" in url.lower() or "yandex.com" in url.lower()

    def connect(self) -> bool:
        """Connect to CalDAV server."""
        try:
            if self.auth_type == "bearer":
                from caldav.requests import HTTPBearerAuth

                self.client = caldav.DAVClient(
                    url=self.url,
                    auth=HTTPBearerAuth(self.password),
                )
            else:
                self.client = caldav.DAVClient(
                    url=self.url,
                    username=self.username,
                    password=self.password,
                )
            self.principal = self.client.principal()
            return True
        except Exception as e:
            raise ConnectionError(f"Failed to connect to CalDAV server: {e}") from e

    def set_google_calendars(
        self, calendars: list[dict], credentials: "Any | None" = None, token_path: str | None = None
    ) -> None:
        """Provide the full Google calendar list and enable the Google Calendar REST API backend.

        When set, all event operations (get, create, delete, search) use the Google
        Calendar REST API instead of CalDAV, enabling access to subscribed and shared
        calendars that Google's CalDAV endpoint cannot reach.

        Args:
            calendars: List of calendar dicts from fetch_google_calendar_list.
            credentials: google.oauth2.credentials.Credentials object for auto-refresh.
            token_path: Path to save refreshed tokens.
        """
        from .google_oauth import GoogleCalendarAPI

        self._google_calendars = calendars
        self._google_api = GoogleCalendarAPI(
            self.password, credentials=credentials, token_path=token_path
        )

    def _check_connected(self) -> None:
        """Raise if not connected."""
        if not self.principal:
            raise RuntimeError("Not connected to CalDAV server. Call connect() first.")

    def _get_calendars(self) -> list[Any]:
        """Return all calendars accessible to the authenticated user."""
        self._check_connected()
        return self.principal.calendars()  # type: ignore[union-attr]

    def _resolve_google_id(
        self, calendar_index: int = 0, calendar_name: str | None = None
    ) -> str:
        """Return the Google Calendar ID for the specified calendar (Google accounts only)."""
        cals = self._google_calendars  # type: ignore[union-attr]
        if calendar_name is not None:
            needle = calendar_name.lower()
            for cal in cals:
                if needle in cal["name"].lower() or needle in cal["id"].lower():
                    return cal["id"]  # type: ignore[no-any-return]
            available = [c["name"] for c in cals]
            raise ValueError(
                f"Calendar '{calendar_name}' not found. Available calendars: {available}"
            )
        if calendar_index >= len(cals):
            raise ValueError(
                f"Calendar index {calendar_index} not found. Available calendars: {len(cals)}"
            )
        return cals[calendar_index]["id"]  # type: ignore[no-any-return]

    def _resolve_calendar(
        self, calendar_index: int = 0, calendar_name: str | None = None
    ) -> Any:
        """Return the calendar matching name (if given) or index.

        If a Google calendar list has been set via set_google_calendars(), uses that
        (enables subscribed/shared calendar access). Otherwise falls back to
        principal.calendars().

        calendar_name takes precedence over calendar_index. Matching is
        case-insensitive and checks both the calendar name and ID/URL, so you can
        pass a partial name or email fragment.

        Raises ValueError if the calendar cannot be found.
        """
        if self._google_calendars is not None:
            cals = self._google_calendars
            if calendar_name is not None:
                needle = calendar_name.lower()
                for cal in cals:
                    if needle in cal["name"].lower() or needle in cal["id"].lower():
                        return self.client.calendar(url=cal["caldav_url"])  # type: ignore[union-attr]
                available = [c["name"] for c in cals]
                raise ValueError(
                    f"Calendar '{calendar_name}' not found. "
                    f"Available calendars: {available}"
                )
            if calendar_index >= len(cals):
                raise ValueError(
                    f"Calendar index {calendar_index} not found. "
                    f"Available calendars: {len(cals)}"
                )
            return self.client.calendar(url=cals[calendar_index]["caldav_url"])  # type: ignore[union-attr]

        # Fallback: CalDAV principal discovery
        calendars = self._get_calendars()
        if calendar_name is not None:
            needle = calendar_name.lower()
            for cal in calendars:
                try:
                    name = cal.name or ""
                except Exception:
                    name = ""
                if needle in name.lower() or needle in str(cal.url).lower():
                    return cal
            available = [getattr(c, "name", str(c.url)) for c in calendars]
            raise ValueError(
                f"Calendar '{calendar_name}' not found. "
                f"Available calendars: {available}"
            )
        if calendar_index >= len(calendars):
            raise ValueError(
                f"Calendar index {calendar_index} not found. "
                f"Available calendars: {len(calendars)}"
            )
        return calendars[calendar_index]

    def list_calendars(self) -> list[CalendarInfo]:
        """Get list of all accessible calendars, including shared/subscribed ones."""
        self._check_connected()

        try:
            if self._google_calendars is not None:
                return [
                    CalendarInfo(index=i, name=c["name"], url=c["caldav_url"])
                    for i, c in enumerate(self._google_calendars)
                ]
            calendars = self._get_calendars()
            result = []
            for i, cal in enumerate(calendars):
                try:
                    name = cal.name or str(cal.url)
                except Exception:
                    name = str(cal.url)
                result.append(CalendarInfo(index=i, name=name, url=str(cal.url)))
            return result
        except Exception as e:
            raise RuntimeError(f"Failed to list calendars: {e}") from e

    def create_event(
        self,
        calendar_index: int = 0,
        calendar_name: str | None = None,
        title: str = "Event",
        description: str = "",
        location: str = "",
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        duration_hours: float = 1.0,
        reminders: list[dict] | None = None,
        attendees: list[AttendeeInput] | None = None,
        categories: list[str] | None = None,
        priority: int | None = None,
        recurrence: dict | None = None,
    ) -> EventCreationResult:
        """
        Create an event in the calendar.

        Args:
            calendar_index: Calendar index (default: 0)
            title: Event title
            description: Event description
            location: Event location
            start_time: Start time (default: tomorrow at 14:00)
            end_time: End time (optional, uses duration_hours if not provided)
            duration_hours: Duration in hours (used if end_time not provided)
            reminders: List of reminder dictionaries with keys:
                - minutes_before: minutes before event
                - action: 'DISPLAY', 'EMAIL', or 'AUDIO'
                - description: reminder text (optional)
            attendees: List of email addresses (str) or dicts with 'email' and 'status'
                Status can be: 'ACCEPTED', 'DECLINED', 'TENTATIVE', 'NEEDS-ACTION'
            categories: List of category strings
            priority: Priority 0-9 (0 = highest, 9 = lowest)
            recurrence: Dictionary with recurrence rules:
                - frequency: 'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'
                - interval: int (optional, default 1)
                - count: int (optional, number of occurrences)
                - until: datetime/date (optional, end date)
                - byday: str (optional, e.g., 'MO,WE,FR')
                - bymonthday: int (optional)
                - bymonth: int (optional)

        Returns:
            Event creation metadata
        """
        self._check_connected()

        if self._google_api is not None:
            cal_id = self._resolve_google_id(calendar_index, calendar_name)
            return self._google_api.create_event(
                calendar_id=cal_id,
                title=title,
                description=description,
                location=location,
                start_time=start_time,
                end_time=end_time,
                duration_hours=duration_hours,
                attendees=attendees,  # type: ignore[arg-type]
                recurrence=recurrence,
            )

        try:
            calendar = self._resolve_calendar(calendar_index, calendar_name)

            # Set default times
            if start_time is None:
                start_time = datetime.now() + timedelta(days=1)
                start_time = start_time.replace(
                    hour=14, minute=0, second=0, microsecond=0
                )
            if end_time is None:
                end_time = start_time + timedelta(hours=duration_hours)

            # Generate unique UID
            uid = f"{int(datetime.now().timestamp())}@caldav-mcp"

            title_escaped = _escape_ical_text(title)
            description_escaped = _escape_ical_text(description)
            location_escaped = _escape_ical_text(location)

            # Format alarm components for reminders
            alarm_components = ""
            if reminders:
                for reminder in reminders:
                    minutes_before = reminder.get("minutes_before", 15)
                    action = reminder.get("action", "DISPLAY").upper()
                    description_text = _escape_ical_text(
                        reminder.get("description", title)
                    )

                    if action == "DISPLAY":
                        alarm_components += f"""BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT{minutes_before}M
DESCRIPTION:{description_text}
END:VALARM
"""
                    elif action == "EMAIL":
                        email_to = reminder.get("email_to", "")
                        alarm_components += f"""BEGIN:VALARM
ACTION:EMAIL
TRIGGER:-PT{minutes_before}M
SUMMARY:{title_escaped}
DESCRIPTION:{description_text}
"""
                        if email_to:
                            alarm_components += f"ATTENDEE:mailto:{email_to}\n"
                        alarm_components += "END:VALARM\n"
                    elif action == "AUDIO":
                        alarm_components += f"""BEGIN:VALARM
ACTION:AUDIO
TRIGGER:-PT{minutes_before}M
DESCRIPTION:{description_text}
END:VALARM
"""

            # Format attendee components
            attendee_components = _format_attendees(attendees) if attendees else ""

            # Format categories
            categories_line = _format_categories(categories) if categories else ""
            if categories_line:
                categories_line += "\n"

            # Format priority
            priority_line = f"PRIORITY:{priority}\n" if priority is not None else ""

            # Format recurrence
            rrule_line = _format_rrule(recurrence) + "\n" if recurrence else ""

            # Format iCalendar data
            vcal_data = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CalDAV MCP Server//Python//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")}
DTSTART:{start_time.strftime("%Y%m%dT%H%M%S")}
DTEND:{end_time.strftime("%Y%m%dT%H%M%S")}
SUMMARY:{title_escaped}
DESCRIPTION:{description_escaped}
LOCATION:{location_escaped}
STATUS:CONFIRMED
SEQUENCE:0
{priority_line}{categories_line}{rrule_line}{attendee_components}{alarm_components}END:VEVENT
END:VCALENDAR"""

            # Save event
            calendar.save_event(vcal_data)

            return {
                "success": True,
                "uid": uid,
                "title": title,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "calendar": calendar.name,
            }

        except Exception as e:
            raise RuntimeError(f"Failed to create event: {e}") from e

    def get_events(
        self,
        calendar_index: int = 0,
        calendar_name: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        include_all_day: bool = True,
    ) -> list[EventRecord]:
        """
        Get events from calendar for specified period.

        Args:
            calendar_index: Calendar index (default: 0)
            start_date: Start of period (default: today 00:00)
            end_date: End of period (default: 7 days from start_date)
            include_all_day: Include all-day events

        Returns:
            List of event dictionaries
        """
        self._check_connected()

        if self._google_api is not None:
            cal_id = self._resolve_google_id(calendar_index, calendar_name)
            return self._google_api.get_events(cal_id, start_date, end_date, include_all_day)

        try:
            calendar = self._resolve_calendar(calendar_index, calendar_name)

            # Set default dates
            if start_date is None:
                start_date = datetime.now().replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            if end_date is None:
                end_date = start_date + timedelta(days=7)

            # Search for events
            events = calendar.date_search(start=start_date, end=end_date)

            result: list[EventRecord] = []
            for event in events:
                try:
                    ical_component = event.icalendar_component

                    summary = ical_component.get("SUMMARY")
                    title = str(summary) if summary else ""

                    desc = ical_component.get("DESCRIPTION")
                    description = str(desc) if desc else ""

                    loc = ical_component.get("LOCATION")
                    location = str(loc) if loc else ""

                    dtstart = ical_component.get("DTSTART")
                    dtend = ical_component.get("DTEND")

                    if dtstart:
                        start_dt = dtstart.dt
                        if isinstance(start_dt, date) and not isinstance(
                            start_dt, datetime
                        ):
                            start_dt = datetime.combine(start_dt, datetime.min.time())
                            all_day = True
                        else:
                            all_day = False
                    else:
                        continue

                    if dtend:
                        end_dt = dtend.dt
                        if isinstance(end_dt, date) and not isinstance(
                            end_dt, datetime
                        ):
                            end_dt = datetime.combine(end_dt, datetime.max.time())
                    else:
                        end_dt = start_dt + timedelta(hours=1)

                    if not include_all_day and all_day:
                        continue

                    # Extract UID
                    uid = str(ical_component.get("UID", ""))

                    # Extract categories
                    cats = ical_component.get("CATEGORIES")
                    categories = _parse_categories(cats)

                    # Extract priority
                    priority = ical_component.get("PRIORITY")
                    priority_value = int(priority) if priority is not None else None

                    # Extract recurrence rule
                    rrule = ical_component.get("RRULE")
                    recurrence = str(rrule) if rrule else None

                    # Extract attendees
                    attendees = _parse_attendees(ical_component)

                    result.append(
                        {
                            "uid": uid,
                            "title": title,
                            "start": start_dt.isoformat(),
                            "end": end_dt.isoformat(),
                            "description": description,
                            "location": location,
                            "all_day": all_day,
                            "categories": categories,
                            "priority": priority_value,
                            "recurrence": recurrence,
                            "attendees": attendees,
                        }
                    )

                except Exception:
                    # Skip events that can't be processed
                    continue

            # Sort by start time
            result.sort(key=lambda x: x["start"])

            return result

        except Exception as e:
            raise RuntimeError(f"Failed to get events: {e}") from e

    def get_today_events(
        self, calendar_index: int = 0, calendar_name: str | None = None
    ) -> list[EventRecord]:
        """Get all events for today."""
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        return self.get_events(calendar_index=calendar_index, calendar_name=calendar_name, start_date=today_start, end_date=today_end)

    def get_week_events(
        self, calendar_index: int = 0, calendar_name: str | None = None, start_from_today: bool = True
    ) -> list[EventRecord]:
        """Get all events for the week."""
        if start_from_today:
            start_date = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            today = datetime.now()
            days_since_monday = today.weekday()
            start_date = (today - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        end_date = start_date + timedelta(days=7)
        return self.get_events(calendar_index=calendar_index, calendar_name=calendar_name, start_date=start_date, end_date=end_date)

    def get_event_by_uid(self, uid: str, calendar_index: int = 0, calendar_name: str | None = None) -> EventRecord | None:
        """
        Get a specific event by its UID.

        Args:
            uid: Event UID
            calendar_index: Calendar index (default: 0)

        Returns:
            Event dictionary or None if not found
        """
        self._check_connected()

        if self._google_api is not None:
            cal_id = self._resolve_google_id(calendar_index, calendar_name)
            return self._google_api.get_event_by_uid(cal_id, uid)

        try:
            calendar = self._resolve_calendar(calendar_index, calendar_name)

            # Search for event by UID
            # Try to search in a wide date range (last year to next year)
            start_date = datetime.now() - timedelta(days=365)
            end_date = datetime.now() + timedelta(days=365)
            events = calendar.date_search(start=start_date, end=end_date)

            for event in events:
                try:
                    ical_component = event.icalendar_component
                    event_uid = str(ical_component.get("UID", ""))
                    if event_uid == uid:
                        # Parse event similar to get_events
                        summary = ical_component.get("SUMMARY")
                        title = str(summary) if summary else ""

                        desc = ical_component.get("DESCRIPTION")
                        description = str(desc) if desc else ""

                        loc = ical_component.get("LOCATION")
                        location = str(loc) if loc else ""

                        dtstart = ical_component.get("DTSTART")
                        dtend = ical_component.get("DTEND")

                        if dtstart:
                            start_dt = dtstart.dt
                            if isinstance(start_dt, date) and not isinstance(
                                start_dt, datetime
                            ):
                                start_dt = datetime.combine(
                                    start_dt, datetime.min.time()
                                )
                                all_day = True
                            else:
                                all_day = False
                        else:
                            continue

                        if dtend:
                            end_dt = dtend.dt
                            if isinstance(end_dt, date) and not isinstance(
                                end_dt, datetime
                            ):
                                end_dt = datetime.combine(end_dt, datetime.max.time())
                        else:
                            end_dt = start_dt + timedelta(hours=1)

                        # Extract additional fields
                        cats = ical_component.get("CATEGORIES")
                        categories = _parse_categories(cats)

                        priority = ical_component.get("PRIORITY")
                        priority_value = int(priority) if priority is not None else None

                        rrule = ical_component.get("RRULE")
                        recurrence = str(rrule) if rrule else None

                        attendees = _parse_attendees(ical_component)

                        return {
                            "uid": uid,
                            "title": title,
                            "start": start_dt.isoformat(),
                            "end": end_dt.isoformat(),
                            "description": description,
                            "location": location,
                            "all_day": all_day,
                            "categories": categories,
                            "priority": priority_value,
                            "recurrence": recurrence,
                            "attendees": attendees,
                        }
                except Exception:
                    continue

            return None

        except Exception as e:
            raise RuntimeError(f"Failed to get event by UID: {e}") from e

    def delete_event(self, uid: str, calendar_index: int = 0, calendar_name: str | None = None) -> EventDeletionResult:
        """
        Delete an event by its UID.

        Args:
            uid: Event UID to delete
            calendar_index: Calendar index (default: 0)

        Returns:
            Dictionary with deletion result
        """
        self._check_connected()

        if self._google_api is not None:
            cal_id = self._resolve_google_id(calendar_index, calendar_name)
            success = self._google_api.delete_event(cal_id, uid)
            if success:
                return {"success": True, "uid": uid, "message": "Event deleted successfully"}
            raise ValueError(f"Event with UID {uid} not found")

        try:
            calendar = self._resolve_calendar(calendar_index, calendar_name)

            # Find the event
            start_date = datetime.now() - timedelta(days=365)
            end_date = datetime.now() + timedelta(days=365)
            events = calendar.date_search(start=start_date, end=end_date)

            for event in events:
                try:
                    ical_component = event.icalendar_component
                    event_uid = str(ical_component.get("UID", ""))
                    if event_uid == uid:
                        event.delete()
                        return {
                            "success": True,
                            "uid": uid,
                            "message": "Event deleted successfully",
                        }
                except Exception:
                    continue

            raise ValueError(f"Event with UID {uid} not found")

        except Exception as e:
            raise RuntimeError(f"Failed to delete event: {e}") from e

    def search_events(
        self,
        calendar_index: int = 0,
        calendar_name: str | None = None,
        query: str | None = None,
        search_fields: list[str] | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[EventRecord]:
        """
        Search events by text, attendees, or location.

        Args:
            calendar_index: Calendar index (default: 0)
            query: Search query string
            search_fields: Fields to search in: 'title', 'description', 'location', 'attendees'
                If None, searches in all fields
            start_date: Start of search period (inclusive)
            end_date: End of search period (exclusive)

        Returns:
            List of matching event dictionaries
        """
        self._check_connected()

        if self._google_api is not None:
            cal_id = self._resolve_google_id(calendar_index, calendar_name)
            return self._google_api.search_events(cal_id, query, search_fields, start_date, end_date)

        if start_date is None or end_date is None:
            raise ValueError(
                "Both start_date and end_date must be provided for search."
            )

        try:
            # Get events in date range

            all_events = self.get_events(
                calendar_index=calendar_index,
                start_date=start_date,
                end_date=end_date,
            )

            if not query:
                return all_events

            query_lower = query.lower()
            if search_fields is None:
                search_fields = ["title", "description", "location", "attendees"]

            results: list[EventRecord] = []
            for event in all_events:
                match = False

                if (
                    (
                        "title" in search_fields
                        and query_lower in event.get("title", "").lower()
                    )
                    or (
                        "description" in search_fields
                        and query_lower in event.get("description", "").lower()
                    )
                    or (
                        "location" in search_fields
                        and query_lower in event.get("location", "").lower()
                    )
                ):
                    match = True
                elif "attendees" in search_fields:
                    attendees = event.get("attendees") or []
                    for attendee in attendees:
                        email = attendee.get("email", "").lower()
                        if query_lower in email:
                            match = True
                            break

                if match:
                    results.append(event)

            return results

        except Exception as e:
            raise RuntimeError(f"Failed to search events: {e}") from e
