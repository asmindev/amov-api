import asyncio
import atexit
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(__file__))

from a2wsgi import ASGIMiddleware
from videasy.app import create_app
from videasy.core.cache import TTLCache
from videasy.core.http_client import create_api_client, create_proxy_client

app = create_app()

# Passenger/WSGI does not trigger ASGI lifespan — initialize state manually
app.state.api_client = create_api_client()
app.state.proxy_client = create_proxy_client()
app.state.client = app.state.api_client
app.state.cache = TTLCache()


def _cleanup_clients():
    """Best-effort synchronous cleanup when Passenger restarts the worker."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_async_cleanup())
    except Exception:
        pass
    finally:
        loop.close()


async def _async_cleanup():
    """Close httpx clients and clear cache to prevent hang-up on restart."""
    try:
        await app.state.api_client.aclose()
    except Exception:
        pass
    try:
        await app.state.proxy_client.aclose()
    except Exception:
        pass
    try:
        app.state.cache.clear()
    except Exception:
        pass


atexit.register(_cleanup_clients)

for _sig in (signal.SIGTERM, signal.SIGINT):
    prev = signal.getsignal(_sig)

    def _handler(signum, _frame, _prev=prev):
        _cleanup_clients()
        if callable(_prev) and _prev not in (signal.SIG_DFL, signal.SIG_IGN):
            _prev(signum, _frame)
        elif _prev == signal.SIG_DFL:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    signal.signal(_sig, _handler)

application = ASGIMiddleware(app)
