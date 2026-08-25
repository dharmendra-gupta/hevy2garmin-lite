"""Shared-token Garmin session management.

Self-contained by design — nothing here imports from the host service, so the
directory can be copied as-is into the other services that share this Garmin
account.
"""

from .session import GarminSession
from .stores import (
    FileTokenStore,
    PostgresTokenStore,
    SqliteTokenStore,
    TokenStore,
)

__all__ = [
    "FileTokenStore",
    "GarminSession",
    "PostgresTokenStore",
    "SqliteTokenStore",
    "TokenStore",
]
