"""Entry point for the standalone merchant application."""
from .production.api import create_app
app=create_app()
