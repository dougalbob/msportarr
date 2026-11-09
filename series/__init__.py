"""
Series registry for the combined MotoGP + Formula 1 app.

Importing this package registers every series module present in it, so the set
of series the app knows about is exactly the set of files here::

    from series import runtime, get_series

    motogp = runtime("motogp")          # materialised enums + tables
    spec = get_series("motogp")         # the declaration itself

``series.base`` holds the declaration types and the registry; each series module
holds one championship's vocabulary.
"""

from series.base import (  # noqa: F401
    ClassSpec,
    Series,
    SeriesRuntime,
    SessionPatternSpec,
    all_series,
    build,
    default_series,
    get_series,
    register,
    runtime,
    series_keys,
)

# Importing the series modules is what registers them: each one calls register()
# at import time, so the set of series the app knows about is exactly the set of
# modules imported here. A new series is a new module and a new line below -
# nothing else in the app changes.
from series import motogp  # noqa: E402,F401

__all__ = [
    "ClassSpec",
    "Series",
    "SeriesRuntime",
    "SessionPatternSpec",
    "all_series",
    "build",
    "default_series",
    "get_series",
    "register",
    "runtime",
    "series_keys",
]
