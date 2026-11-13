"""Cardinal plugin entry point for W1bbex Hub."""

from __future__ import annotations

from .handlers import custom_init, init_cardinal, post_init_cardinal
from .runtime import (
    COMMANDS,
    CREDITS,
    DESCRIPTION,
    NAME,
    PLUGIN_ID,
    SETTINGS_PAGE,
    UUID,
    VERSION,
)

BIND_TO_PRE_INIT = [custom_init]
BIND_TO_POST_INIT = [post_init_cardinal]
BIND_TO_NEW_MESSAGE = []
BIND_TO_DELETE = []

__all__ = [
    "NAME",
    "VERSION",
    "DESCRIPTION",
    "CREDITS",
    "PLUGIN_ID",
    "SETTINGS_PAGE",
    "UUID",
    "COMMANDS",
    "BIND_TO_PRE_INIT",
    "BIND_TO_POST_INIT",
    "BIND_TO_NEW_MESSAGE",
    "BIND_TO_DELETE",
    "custom_init",
    "post_init_cardinal",
    "init_cardinal",
]
