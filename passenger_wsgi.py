import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from a2wsgi import ASGIMiddleware
from videasy.app import create_app

application = ASGIMiddleware(create_app())
