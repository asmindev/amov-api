import os
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

application = ASGIMiddleware(app)
