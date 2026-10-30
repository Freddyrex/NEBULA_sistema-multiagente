"""Aplicación independiente (solo esta API), ideal para el workshop, Swagger y Postman.

    uvicorn nebula.controller.api.standalone:app --reload --port 8000
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from nebula.controller.api import OPENAPI_TAGS, include_api, init_api


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_api()
    yield


app = FastAPI(
    title="NEBULA API",
    description="REST API (JWT + OAuth 2.0) para gestionar experimentos de aprendizaje federado en NEBULA.",
    version="1.0.0",
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)
include_api(app)


@app.get("/", tags=["Public"], summary="API root")
def root():
    return {"message": "NEBULA API", "version": "1.0.0"}
