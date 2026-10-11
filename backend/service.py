"""Deployment entry point. Simulation remains available as backend.main for regression tests."""
from .production.api import create_app
app=create_app()
