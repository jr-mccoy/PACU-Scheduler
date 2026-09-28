"""Web interface for the PACU scheduler, for phones and other computers.

Run it with ``python -m web``; ``docs/web-server.md`` covers setting it up as
an always-on server reached over Tailscale.
"""

from .app import create_app

__all__ = ["create_app"]
