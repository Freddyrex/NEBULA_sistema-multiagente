"""Lightweight health-check API: the small component used for Workshop 6.

Run it locally with:

    python -m uvicorn nebula.healthcheck.app:app --port 8010

Then check the endpoint:

    curl http://127.0.0.1:8010/health
"""
import os
from fastapi import FastAPI

app = FastAPI(title="NEBULA Health API", version="1.1.0")


@app.get("/health", tags=["Health"], summary="Service health status")
def health() -> dict[str, str]:
    """Return the current health status of the service."""
    return {"status": "ok", "service": "nebula", "version": "1.1.0"}
