from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.db import apply_schema, connect


@asynccontextmanager
async def lifespan(app):
    with connect() as conn:
        apply_schema(conn)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
    except psycopg.Error:
        return JSONResponse({"status": "db unavailable"}, status_code=503)
    return {"status": "ok"}
