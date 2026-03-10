"""MCP server for CalDAV calendar integration."""

import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from .client import CalDAVClient

# Configure logging
logger = logging.getLogger("mcp-caldav")

DEFAULT_ACCOUNTS_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "mcp-caldav", "accounts.json"
)


@dataclass
class AppContext:
    """Application context for MCP CalDAV."""

    clients: dict[str, CalDAVClient] = field(default_factory=dict)


def _load_accounts_config() -> list[dict[str, Any]]:
    """Load account configs from accounts.json, falling back to env vars."""
    config_path = os.getenv("CALDAV_ACCOUNTS_CONFIG", DEFAULT_ACCOUNTS_PATH)

    if os.path.exists(config_path):
        with open(config_path) as f:
            data = json.load(f)
        accounts = data.get("accounts", [])
        if accounts:
            return accounts

    # Fallback: single account from env vars
    url = os.getenv("CALDAV_URL")
    if not url:
        return []

    account: dict[str, Any] = {
        "name": "default",
        "url": url,
        "auth_type": os.getenv("CALDAV_AUTH_TYPE", "basic"),
    }
    if account["auth_type"] == "basic":
        account["username"] = os.getenv("CALDAV_USERNAME") or os.getenv("YANDEX_USERNAME") or ""
        account["password"] = os.getenv("CALDAV_PASSWORD") or os.getenv("YANDEX_PASSWORD") or ""
    if os.getenv("GOOGLE_CLIENT_ID"):
        account["google_client_id"] = os.getenv("GOOGLE_CLIENT_ID")
    if os.getenv("GOOGLE_CLIENT_SECRET"):
        account["google_client_secret"] = os.getenv("GOOGLE_CLIENT_SECRET")
    if os.getenv("GOOGLE_CLIENT_SECRETS_FILE"):
        account["google_client_secrets_file"] = os.getenv("GOOGLE_CLIENT_SECRETS_FILE")
    if os.getenv("GOOGLE_TOKEN_PATH"):
        account["google_token_path"] = os.getenv("GOOGLE_TOKEN_PATH")

    return [account]


def _extract_google_email(url: str) -> str | None:
    """Extract the user email from a Google CalDAV URL."""
    import re
    from urllib.parse import unquote

    match = re.search(r"/caldav/v2/([^/]+)", url)
    if match:
        return unquote(match.group(1))
    return None


def _create_client(account: dict[str, Any]) -> CalDAVClient:
    """Create a CalDAVClient from an account config dict."""
    url = account["url"]
    auth_type = account.get("auth_type", "basic")
    name = account.get("name", "default")

    if auth_type == "oauth":
        from .google_oauth import fetch_google_calendar_list, get_google_access_token

        # Default token path per account
        default_token = os.path.join(
            os.path.expanduser("~"), ".config", "mcp-caldav", f"{name}_token.json"
        )
        token_path = account.get("google_token_path", default_token)

        access_token = get_google_access_token(
            client_id=account.get("google_client_id"),
            client_secret=account.get("google_client_secret"),
            client_secrets_file=account.get("google_client_secrets_file"),
            token_path=token_path,
        )
        client = CalDAVClient(url=url, password=access_token, auth_type="bearer")

        # Populate the full calendar list via Google Calendar REST API so that
        # subscribed and shared calendars are visible alongside owned calendars.
        user_email = _extract_google_email(url)
        if user_email:
            try:
                calendars = fetch_google_calendar_list(access_token, user_email)
                client.set_google_calendars(calendars)
                logger.debug(f"Loaded {len(calendars)} Google calendars for {user_email}")
            except Exception as e:
                logger.warning(f"Failed to fetch Google calendar list for {user_email}: {e}")

        return client
    else:
        username = account.get("username", "")
        password = account.get("password", "")
        return CalDAVClient(url=url, username=username, password=password)


@asynccontextmanager
async def server_lifespan(server: Server) -> AsyncIterator[AppContext]:  # noqa: ARG001
    """Initialize and clean up application resources."""
    accounts = _load_accounts_config()
    clients: dict[str, CalDAVClient] = {}

    for account in accounts:
        name = account.get("name", "default")
        try:
            client = _create_client(account)
            client.connect()
            clients[name] = client
            logger.info(f"Connected account '{name}': {account['url']}")
        except Exception as e:
            logger.error(f"Failed to connect account '{name}': {e}")

    if not clients and not accounts:
        logger.warning(
            "No CalDAV accounts configured. "
            "Create ~/.config/mcp-caldav/accounts.json or set CALDAV_URL env var."
        )

    try:
        yield AppContext(clients=clients)
    finally:
        pass


# Create server instance
app = Server("mcp-caldav", lifespan=server_lifespan)


def _account_param(ctx: AppContext) -> dict[str, Any]:
    """Build account parameter schema with connected account names."""
    names = list(ctx.clients.keys())
    return {
        "type": "string",
        "description": f"Account name. Connected accounts: {names}. Omit to use '{names[0]}'.",
        "enum": names,
    }


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available CalDAV tools."""
    ctx = app.request_context.lifespan_context

    if not ctx or not ctx.clients:
        return []

    acct_param = _account_param(ctx)
    acct_names = list(ctx.clients.keys())
    acct_desc = ", ".join(acct_names)

    tools = [
        Tool(
            name="caldav_list_accounts",
            description=(
                f"List all connected calendar accounts and the calendars available in each. "
                f"Connected accounts: {acct_desc}. "
                "Use this to get an overview of all accounts and their calendar names/indices."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="caldav_list_calendars",
            description=(
                f"List all calendars accessible to an account ({acct_desc}). "
                "For Google OAuth accounts this uses the Google Calendar API and returns every calendar "
                "visible in the Google Calendar sidebar — including the user's own calendars, "
                "other people's calendars they have access to, shared team calendars, and subscribed calendars. "
                "Use this to discover calendar names before calling other tools with calendar_name. "
                "The returned 'name' and 'index' can be used to identify a specific calendar."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": acct_param,
                },
            },
        ),
        Tool(
            name="caldav_create_event",
            description=(
                f"Create a new calendar event in an account ({acct_desc}). "
                "Specify the target calendar via calendar_name (e.g. 'work', 'personal', a coworker's name) "
                "or calendar_index. Supports title, description, location, start/end times, duration, "
                "attendees, reminders, categories, priority, and recurrence rules."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": acct_param,
                    "calendar_index": {
                        "type": "integer",
                        "description": "Index of the calendar (default: 0)",
                        "default": 0,
                    },
                    "calendar_name": {
                        "type": "string",
                        "description": "Calendar name (or partial name/email). Takes precedence over calendar_index. Use caldav_list_calendars to see available calendars.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Event title",
                    },
                    "description": {
                        "type": "string",
                        "description": "Event description",
                        "default": "",
                    },
                    "location": {
                        "type": "string",
                        "description": "Event location",
                        "default": "",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start time in ISO format (e.g., '2025-01-20T14:00:00'). "
                        "If not provided, defaults to tomorrow at 14:00",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time in ISO format (e.g., '2025-01-20T15:00:00'). "
                        "If not provided, uses duration_hours from start_time",
                    },
                    "duration_hours": {
                        "type": "number",
                        "description": "Duration in hours (used if end_time not provided)",
                        "default": 1.0,
                    },
                    "reminders": {
                        "type": "array",
                        "description": "List of reminders. Each reminder is an object with: "
                        "minutes_before (integer), action ('DISPLAY', 'EMAIL', or 'AUDIO'), "
                        "and optional description (string)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "minutes_before": {"type": "integer"},
                                "action": {
                                    "type": "string",
                                    "enum": ["DISPLAY", "EMAIL", "AUDIO"],
                                },
                                "description": {"type": "string"},
                            },
                            "required": ["minutes_before", "action"],
                        },
                    },
                    "attendees": {
                        "type": "array",
                        "description": "List of attendee email addresses (strings) or objects with 'email' and 'status' (ACCEPTED/DECLINED/TENTATIVE/NEEDS-ACTION)",
                        "items": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "email": {"type": "string"},
                                        "status": {
                                            "type": "string",
                                            "enum": [
                                                "ACCEPTED",
                                                "DECLINED",
                                                "TENTATIVE",
                                                "NEEDS-ACTION",
                                            ],
                                        },
                                    },
                                    "required": ["email"],
                                },
                            ]
                        },
                    },
                    "categories": {
                        "type": "array",
                        "description": "List of category/tag strings",
                        "items": {"type": "string"},
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Priority 0-9 (0 = highest, 9 = lowest)",
                        "minimum": 0,
                        "maximum": 9,
                    },
                    "recurrence": {
                        "type": "object",
                        "description": "Recurrence rule for repeating events",
                        "properties": {
                            "frequency": {
                                "type": "string",
                                "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"],
                                "description": "How often the event repeats",
                            },
                            "interval": {
                                "type": "integer",
                                "description": "Interval between occurrences (default: 1)",
                                "default": 1,
                            },
                            "count": {
                                "type": "integer",
                                "description": "Number of occurrences",
                            },
                            "until": {
                                "type": "string",
                                "description": "End date in ISO format",
                            },
                            "byday": {
                                "type": "string",
                                "description": "Days of week (e.g., 'MO,WE,FR' for Monday, Wednesday, Friday)",
                            },
                            "bymonthday": {
                                "type": "integer",
                                "description": "Day of month (1-31)",
                            },
                            "bymonth": {
                                "type": "integer",
                                "description": "Month (1-12)",
                            },
                        },
                        "required": ["frequency"],
                    },
                },
                "required": ["title"],
            },
        ),
        Tool(
            name="caldav_get_event_by_uid",
            description=(
                f"Fetch the full details of a single event by its UID from an account ({acct_desc}). "
                "The UID is returned in the 'uid' field of events from caldav_get_events, "
                "caldav_get_today_events, caldav_get_week_events, or caldav_search_events. "
                "Use this to retrieve complete event details including description and attendees."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": acct_param,
                    "uid": {
                        "type": "string",
                        "description": "Event UID",
                    },
                    "calendar_index": {
                        "type": "integer",
                        "description": "Index of the calendar (default: 0)",
                        "default": 0,
                    },
                    "calendar_name": {
                        "type": "string",
                        "description": "Calendar name (or partial name/email). Takes precedence over calendar_index. Use caldav_list_calendars to see available calendars.",
                    },
                },
                "required": ["uid"],
            },
        ),
        Tool(
            name="caldav_delete_event",
            description=(
                f"Permanently delete a calendar event by its UID from an account ({acct_desc}). "
                "The UID is returned in the 'uid' field of events from other get/search tools. "
                "This action is irreversible — confirm with the user before calling."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": acct_param,
                    "uid": {
                        "type": "string",
                        "description": "Event UID to delete",
                    },
                    "calendar_index": {
                        "type": "integer",
                        "description": "Index of the calendar (default: 0)",
                        "default": 0,
                    },
                    "calendar_name": {
                        "type": "string",
                        "description": "Calendar name (or partial name/email). Takes precedence over calendar_index. Use caldav_list_calendars to see available calendars.",
                    },
                },
                "required": ["uid"],
            },
        ),
        Tool(
            name="caldav_search_events",
            description=(
                f"Search for events in a calendar by keyword, attendee email, or location ({acct_desc}). "
                "Requires a date range (start_date and end_date). For Google accounts, title and description "
                "searches run server-side for speed; location and attendee searches filter client-side. "
                "Use search_fields to narrow which fields are searched. "
                "Useful for finding events involving a specific person or topic within a time window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": acct_param,
                    "calendar_index": {
                        "type": "integer",
                        "description": "Index of the calendar (default: 0)",
                        "default": 0,
                    },
                    "calendar_name": {
                        "type": "string",
                        "description": "Calendar name (or partial name/email). Takes precedence over calendar_index. Use caldav_list_calendars to see available calendars.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query string",
                    },
                    "search_fields": {
                        "type": "array",
                        "description": "Fields to search in: 'title', 'description', 'location', 'attendees'. If not provided, searches in all fields",
                        "items": {
                            "type": "string",
                            "enum": ["title", "description", "location", "attendees"],
                        },
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date for search period in ISO format",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date for search period in ISO format",
                    },
                },
                "required": ["start_date", "end_date"],
            },
        ),
        Tool(
            name="caldav_get_events",
            description=(
                f"Get all events in a specific calendar for a given date range ({acct_desc}). "
                "Defaults to today through 7 days out if no dates provided. "
                "Specify calendar_name (e.g. 'erik', 'deepthi', 'work') to query a specific person's "
                "or team's calendar. Use caldav_list_calendars to see all available calendar names. "
                "For checking someone's schedule or availability, prefer caldav_get_today_events or "
                "caldav_get_week_events for convenience."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": acct_param,
                    "calendar_index": {
                        "type": "integer",
                        "description": "Index of the calendar (default: 0)",
                        "default": 0,
                    },
                    "calendar_name": {
                        "type": "string",
                        "description": "Calendar name (or partial name/email). Takes precedence over calendar_index. Use caldav_list_calendars to see available calendars.",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date in ISO format (e.g., '2025-01-20T00:00:00'). "
                        "Defaults to today 00:00",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in ISO format (e.g., '2025-01-27T23:59:59'). "
                        "Defaults to 7 days from start_date",
                    },
                    "include_all_day": {
                        "type": "boolean",
                        "description": "Include all-day events",
                        "default": True,
                    },
                },
            },
        ),
        Tool(
            name="caldav_get_today_events",
            description=(
                f"Get all events scheduled for today from a calendar ({acct_desc}). "
                "Use calendar_name to check a specific person's or team's calendar "
                "(e.g. 'erik', 'deepthi', 'Stafl Systems Master Calendar'). "
                "Omit calendar_name to get the account owner's primary calendar."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": acct_param,
                    "calendar_index": {
                        "type": "integer",
                        "description": "Index of the calendar (default: 0)",
                        "default": 0,
                    },
                    "calendar_name": {
                        "type": "string",
                        "description": "Calendar name (or partial name/email). Takes precedence over calendar_index. Use caldav_list_calendars to see available calendars.",
                    },
                },
            },
        ),
        Tool(
            name="caldav_get_week_events",
            description=(
                f"Get all events for the next 7 days (or current Mon–Sun week) from a calendar ({acct_desc}). "
                "Use start_from_today=true (default) to get a rolling 7-day window from today, "
                "or start_from_today=false to get the current calendar week (Monday through Sunday). "
                "Use calendar_name to check a specific person's or team's calendar."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account": acct_param,
                    "calendar_index": {
                        "type": "integer",
                        "description": "Index of the calendar (default: 0)",
                        "default": 0,
                    },
                    "calendar_name": {
                        "type": "string",
                        "description": "Calendar name (or partial name/email). Takes precedence over calendar_index. Use caldav_list_calendars to see available calendars.",
                    },
                    "start_from_today": {
                        "type": "boolean",
                        "description": "Start from today (True) or from Monday (False)",
                        "default": True,
                    },
                },
            },
        ),
    ]

    return tools


def _get_client(ctx: AppContext, account: str | None) -> CalDAVClient:
    """Resolve a client by account name, defaulting to the first one."""
    if not ctx.clients:
        raise RuntimeError("No CalDAV accounts connected.")

    if account:
        if account not in ctx.clients:
            available = ", ".join(ctx.clients.keys())
            raise ValueError(f"Account '{account}' not found. Available: {available}")
        return ctx.clients[account]

    # Default to first account
    return next(iter(ctx.clients.values()))


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> Sequence[TextContent]:
    """Handle tool calls for CalDAV operations."""
    ctx = app.request_context.lifespan_context

    if not ctx or not ctx.clients:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": "No CalDAV accounts connected. Check server logs."},
                    indent=2,
                ),
            )
        ]

    try:
        account = arguments.get("account") if arguments else None

        if name == "caldav_list_accounts":
            account_list = []
            for acct_name, client in ctx.clients.items():
                cals = client.list_calendars()
                account_list.append({
                    "name": acct_name,
                    "url": client.url,
                    "calendars": cals,
                })
            return [
                TextContent(
                    type="text",
                    text=json.dumps(account_list, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "caldav_list_calendars":
            client = _get_client(ctx, account)
            calendars = client.list_calendars()
            return [
                TextContent(
                    type="text",
                    text=json.dumps(calendars, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "caldav_create_event":
            client = _get_client(ctx, account)
            calendar_index = arguments.get("calendar_index", 0)
            calendar_name = arguments.get("calendar_name")
            title = arguments.get("title")
            description = arguments.get("description", "")
            location = arguments.get("location", "")
            start_time_str = arguments.get("start_time")
            end_time_str = arguments.get("end_time")
            duration_hours = arguments.get("duration_hours", 1.0)
            reminders = arguments.get("reminders")
            attendees = arguments.get("attendees")
            categories = arguments.get("categories")
            priority = arguments.get("priority")
            recurrence = arguments.get("recurrence")

            start_time = None
            if start_time_str:
                start_time = datetime.fromisoformat(
                    start_time_str.replace("Z", "+00:00")
                )

            end_time = None
            if end_time_str:
                end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))

            # Parse recurrence until date if provided
            if recurrence and recurrence.get("until"):
                until_str = recurrence["until"]
                try:
                    recurrence["until"] = datetime.fromisoformat(
                        until_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    # Try date format
                    from contextlib import suppress

                    with suppress(ValueError):
                        recurrence["until"] = datetime.fromisoformat(until_str).date()

            result = client.create_event(
                calendar_index=calendar_index,
                calendar_name=calendar_name,
                title=title,
                description=description,
                location=location,
                start_time=start_time,
                end_time=end_time,
                duration_hours=duration_hours,
                reminders=reminders,
                attendees=attendees,
                categories=categories,
                priority=priority,
                recurrence=recurrence,
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "caldav_get_events":
            client = _get_client(ctx, account)
            calendar_index = arguments.get("calendar_index", 0)
            calendar_name = arguments.get("calendar_name")
            start_date_str = arguments.get("start_date")
            end_date_str = arguments.get("end_date")
            include_all_day = arguments.get("include_all_day", True)

            start_date = None
            if start_date_str:
                start_date = datetime.fromisoformat(
                    start_date_str.replace("Z", "+00:00")
                )
            end_date = None
            if end_date_str:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))

            events = client.get_events(
                calendar_index=calendar_index,
                calendar_name=calendar_name,
                start_date=start_date,
                end_date=end_date,
                include_all_day=include_all_day,
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(events, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "caldav_get_today_events":
            client = _get_client(ctx, account)
            calendar_index = arguments.get("calendar_index", 0)
            calendar_name = arguments.get("calendar_name")
            events = client.get_today_events(calendar_index=calendar_index, calendar_name=calendar_name)

            return [
                TextContent(
                    type="text",
                    text=json.dumps(events, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "caldav_get_week_events":
            client = _get_client(ctx, account)
            calendar_index = arguments.get("calendar_index", 0)
            calendar_name = arguments.get("calendar_name")
            start_from_today = arguments.get("start_from_today", True)
            events = client.get_week_events(
                calendar_index=calendar_index, calendar_name=calendar_name, start_from_today=start_from_today
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(events, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "caldav_get_event_by_uid":
            client = _get_client(ctx, account)
            uid = arguments.get("uid")
            calendar_index = arguments.get("calendar_index", 0)
            calendar_name = arguments.get("calendar_name")

            event = client.get_event_by_uid(uid=uid, calendar_index=calendar_index, calendar_name=calendar_name)

            if event:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(event, indent=2, ensure_ascii=False),
                    )
                ]
            else:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": f"Event with UID {uid} not found"}, indent=2
                        ),
                    )
                ]

        elif name == "caldav_delete_event":
            client = _get_client(ctx, account)
            uid = arguments.get("uid")
            calendar_index = arguments.get("calendar_index", 0)
            calendar_name = arguments.get("calendar_name")

            result = client.delete_event(uid=uid, calendar_index=calendar_index, calendar_name=calendar_name)

            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "caldav_search_events":
            client = _get_client(ctx, account)
            calendar_index = arguments.get("calendar_index", 0)
            calendar_name = arguments.get("calendar_name")
            query = arguments.get("query")
            search_fields = arguments.get("search_fields")
            start_date_str = arguments.get("start_date")
            end_date_str = arguments.get("end_date")

            if not start_date_str or not end_date_str:
                raise ValueError(
                    "caldav_search_events requires both start_date and end_date arguments."
                )

            start_date = None
            if start_date_str:
                start_date = datetime.fromisoformat(
                    start_date_str.replace("Z", "+00:00")
                )

            end_date = None
            if end_date_str:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))

            events = client.search_events(
                calendar_index=calendar_index,
                calendar_name=calendar_name,
                query=query,
                search_fields=search_fields,
                start_date=start_date,
                end_date=end_date,
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(events, indent=2, ensure_ascii=False),
                )
            ]

        else:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unknown tool: {name}"}, indent=2),
                )
            ]

    except Exception as e:
        logger.error(f"Error calling tool {name}: {e}", exc_info=True)
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e)}, indent=2),
            )
        ]


async def run_server(transport: str = "stdio", port: int = 8000) -> None:
    """Run the MCP CalDAV server with the specified transport."""
    if transport == "sse":
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.routing import Mount, Route

        sse = SseServerTransport("/messages/")

        async def handle_sse(request: Request) -> None:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await app.run(
                    streams[0], streams[1], app.create_initialization_options()
                )

        starlette_app = Starlette(
            debug=True,
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ],
        )

        import uvicorn

        config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port)
        server = uvicorn.Server(config)
        await server.serve()
    else:
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream, write_stream, app.create_initialization_options()
            )
