"""Vercel's FastAPI entrypoint; local development uses backend.app.main:app."""

from backend.app.main import app

__all__ = ["app"]
