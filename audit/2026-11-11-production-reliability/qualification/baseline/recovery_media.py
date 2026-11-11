"""Compose the existing Media app without changing acknowledgment or workflows."""
from main import app
from recovery_routes import configured_router

app.include_router(configured_router())
