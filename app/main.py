"""ASGI composition entrypoint for the API process."""

from app.api import create_app
from app.config import Settings


# Settings validation intentionally fails startup when deployment identity and
# CSRF configuration are absent or still contain placeholders.
app = create_app(Settings())
