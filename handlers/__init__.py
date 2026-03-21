"""Lightweight package initializer for `handlers`.

Deliberately avoids importing submodules at package import time to
prevent circular import problems during application startup. Import
submodules directly (e.g. `from handlers.base_handlers import ...`) as
needed.
"""

__all__ = []
