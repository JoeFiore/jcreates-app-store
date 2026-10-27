"""Docker entrypoint for MC Dashboard — no ModLink support in this build.

Imports the normal app, then removes every public /mods route (the page,
the API, and the bundle download) so they simply do not exist here.
The dashboard itself (console, files, players, backups, logs, import)
is unchanged. Set MODS_ENABLED=false in docker-compose.yml as well so
the frontend hides the Mods tab.
"""
from .app import app  # noqa: F401  (re-exported)

# FastAPI's routes property has no setter, so filter the router's list
# in place (and the mounted frontend route, added last, is kept).
app.router.routes[:] = [
    r for r in app.router.routes
    if not (getattr(r, "path", "").startswith("/mods")
            or getattr(r, "path", "").startswith("/api/mods"))
]
