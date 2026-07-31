import os

# Environment variables (.env) are loaded by logging_config.py, which calls
# load_dotenv() at module level.  logging_config.py MUST be imported BEFORE
# any module that reads os.getenv(),
# otherwise env vars will not be populated yet.
#
# Both bot.py and main.py import logging_config as their first import,
# so this ordering is guaranteed in production.  Test files that import
# config.py directly should either import logging_config first or patch
# os.environ explicitly.


def env_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to *default* on unset/garbage."""
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to *default* on unset/garbage."""
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def get_owner_id() -> int | None:
    """Return the configured bot owner's Telegram user ID, or None if not set.

    SECURITY: When BOT_OWNER_ID is not configured, this returns None so that
    callers can deny access to ALL users (fail-closed) rather than allowing
    everyone. Never returns a non-integer value.
    """
    raw = os.getenv("BOT_OWNER_ID")
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except (ValueError, TypeError):
        return None


def is_owner(user_id: int | None) -> bool:
    """Check whether *user_id* is the configured bot owner.

    Returns False when:
    - user_id is None
    - BOT_OWNER_ID is not configured (fail-closed — deny all)
    - user_id does not match BOT_OWNER_ID
    """
    owner_id = get_owner_id()
    if owner_id is None:
        return False
    if user_id is None:
        return False
    return user_id == owner_id
