import os


# ----------  env helpers  ----------
def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# ----------  owner helpers  ----------
def get_owner_id() -> int | None:
    raw = os.getenv("BOT_OWNER_ID")
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except (ValueError, TypeError):
        return None


def is_owner(user_id: int | None) -> bool:
    owner_id = get_owner_id()
    if owner_id is None:
        return False
    if user_id is None:
        return False
    return user_id == owner_id
