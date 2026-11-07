"""Event-loop factory for uvicorn (`--loop app.core.loop:selector_loop_factory`).

psycopg's async mode cannot run on Windows' default ProactorEventLoop, so we always
use a SelectorEventLoop. On Linux/macOS this is uvicorn's default anyway.
"""

import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop()
