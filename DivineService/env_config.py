import logging
import os

logger = logging.getLogger(__name__)


def positive_int_env(name: str, default: int) -> int:
    """Integer >= 1 from the environment, or `default`. A bad value (typo, 0,
    negative, "10/min") logs a warning and falls back instead of crashing app
    import or silently disabling/zeroing a limit."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning("invalid %s=%r - using default %s", name, raw, default)
        return default
    return value
