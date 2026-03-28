"""MCP CalDAV Server - Calendar integration for MCP."""

import asyncio
import logging
import os

import click
from dotenv import load_dotenv

__version__ = "0.1.0"

# Initialize logging
logging_level = logging.WARNING
if os.getenv("MCP_VERBOSE", "").lower() in ("true", "1", "yes"):
    logging_level = logging.DEBUG

logging.basicConfig(
    level=logging_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("mcp-caldav")


@click.group(invoke_without_command=True)
@click.pass_context
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase verbosity (can be used multiple times)",
)
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to .env file",
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    help="Transport type (stdio or sse)",
)
@click.option(
    "--port",
    default=8000,
    help="Port to listen on for SSE transport",
)
@click.option(
    "--caldav-url",
    help="CalDAV server URL (e.g., https://caldav.example.com/)",
)
@click.option("--caldav-username", help="CalDAV username")
@click.option("--caldav-password", help="CalDAV password or app password")
def main(
    ctx: click.Context,
    verbose: bool,
    env_file: str | None,
    transport: str,
    port: int,
    caldav_url: str | None,
    caldav_username: str | None,
    caldav_password: str | None,
) -> None:
    """MCP CalDAV Server - Universal calendar functionality for MCP

    Works with any CalDAV-compatible calendar server.
    """
    # Configure logging based on verbosity
    logging_level = logging.WARNING
    if verbose == 1:
        logging_level = logging.INFO
    elif verbose >= 2:
        logging_level = logging.DEBUG

    logging.getLogger("mcp-caldav").setLevel(logging_level)

    # Load environment variables from file if specified
    if env_file:
        logger.debug(f"Loading environment from file: {env_file}")
        load_dotenv(env_file)
    else:
        logger.debug("Attempting to load environment from default .env file")
        load_dotenv()

    # Set environment variables from command line arguments if provided
    if caldav_url:
        os.environ["CALDAV_URL"] = caldav_url
    if caldav_username:
        os.environ["CALDAV_USERNAME"] = caldav_username
    if caldav_password:
        os.environ["CALDAV_PASSWORD"] = caldav_password

    # If no subcommand, run the server (default behavior)
    if ctx.invoked_subcommand is None:
        from . import server

        asyncio.run(server.run_server(transport=transport, port=port))


@main.command()
@click.option("--account", "-a", help="Account name from accounts.json (default: all oauth accounts)")
@click.option("--client-id", help="Google OAuth client ID (override)")
@click.option("--client-secret", help="Google OAuth client secret (override)")
@click.option("--client-secrets-file", help="Path to Google client_secrets.json")
@click.option("--token-path", help="Path to save the token")
def auth(
    account: str | None,
    client_id: str | None,
    client_secret: str | None,
    client_secrets_file: str | None,
    token_path: str | None,
) -> None:
    """Authenticate with Google OAuth (run this once before starting the server)."""
    load_dotenv()

    from .google_oauth import get_google_access_token
    from .server import _load_accounts_config

    accounts = _load_accounts_config()
    oauth_accounts = [a for a in accounts if a.get("auth_type") == "oauth"]

    if account:
        oauth_accounts = [a for a in oauth_accounts if a.get("name") == account]
        if not oauth_accounts:
            click.echo(f"No OAuth account named '{account}' found in config.")
            return

    if not oauth_accounts:
        # Fallback: no accounts.json, use CLI args / env vars
        click.echo("No OAuth accounts in config. Using CLI args...")
        cid = client_id or os.getenv("GOOGLE_CLIENT_ID")
        csecret = client_secret or os.getenv("GOOGLE_CLIENT_SECRET")
        csfile = client_secrets_file or os.getenv("GOOGLE_CLIENT_SECRETS_FILE")
        tpath = token_path or os.getenv("GOOGLE_TOKEN_PATH")
        _token, _creds = get_google_access_token(
            client_id=cid,
            client_secret=csecret,
            client_secrets_file=csfile,
            token_path=tpath,
            force_new=True,
        )
        click.echo("Authentication successful! Token saved.")
        return

    for acct in oauth_accounts:
        name = acct.get("name", "default")
        default_token = os.path.join(
            os.path.expanduser("~"), ".config", "mcp-caldav", f"{name}_token.json"
        )
        tpath = token_path or acct.get("google_token_path", default_token)
        cid = client_id or acct.get("google_client_id")
        csecret = client_secret or acct.get("google_client_secret")
        csfile = client_secrets_file or acct.get("google_client_secrets_file")

        click.echo(f"Authenticating account '{name}'...")
        _token, _creds = get_google_access_token(
            client_id=cid,
            client_secret=csecret,
            client_secrets_file=csfile,
            token_path=tpath,
            force_new=True,
        )
        click.echo(f"Account '{name}' authenticated! Token saved to {tpath}")


__all__ = ["__version__", "main", "server"]

if __name__ == "__main__":
    main()
