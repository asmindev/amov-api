from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from videasy.models.common import ErrorDetail

logger = logging.getLogger("videasy")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def global_exception(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception")
        return JSONResponse(
            status_code=500,
            content=ErrorDetail(error="internal_error", detail=str(exc)).model_dump(),
        )
