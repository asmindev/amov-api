from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send


logger = logging.getLogger("videasy")


class RequestFormatter(logging.Formatter):
    """Custom formatter:
    [timestamp] [LEVEL] [METHOD] [entry file:line] [route]

    - METHOD is only shown for request-related logs (GET, POST, etc.)
    - route is only shown for HTTP logs (e.g. /sources, /proxy)
    - Non-request logs omit METHOD and route brackets
    """

    LEVEL_COLORS = {
        logging.DEBUG: "\033[36m",      # cyan
        logging.INFO: "\033[32m",       # green
        logging.WARNING: "\033[33m",    # yellow
        logging.ERROR: "\033[31m",      # red
        logging.CRITICAL: "\033[1;31m", # bold red
    }
    RESET = "\033[0m"

    def __init__(self, use_colors: bool = True) -> None:
        super().__init__()
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f")[:-3]
        level = record.levelname
        module = getattr(record, "module", record.name)
        func = getattr(record, "funcName", "")
        lineno = getattr(record, "lineno", "")
        method = getattr(record, "http_method", "")
        route = getattr(record, "http_route", "")
        status = getattr(record, "http_status", "")
        client = getattr(record, "client_host", "")
        duration = getattr(record, "duration_ms", "")

        entry = f"{module}.py" if func else f"{module}.py"
        if func:
            entry = f"{module}/{func}.py"
        if lineno:
            entry += f":{lineno}"

        if method and route:
            request_block = f"[{method}] [{entry}] [{route}]"
            if status:
                request_block += f" [{status}]"
            if duration:
                request_block += f" [{duration}ms]"
            if client:
                request_block += f" [{client}]"
        else:
            request_block = f"[{entry}]"

        msg = record.getMessage()

        if self.use_colors:
            color = self.LEVEL_COLORS.get(record.levelno, "")
            return f"[{ts}] [{color}{level}{self.RESET}] {request_block} {msg}"

        return f"[{ts}] [{level}] {request_block} {msg}"


class FileFormatter(logging.Formatter):
    """Plain text formatter for file output (no ANSI colors).
    [2024-01-15T10:30:45.123] [INFO] [GET] [sources/service.py:112] [GET /sources] [127.0.0.1] msg
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f")[:-3]
        level = record.levelname
        module = getattr(record, "module", record.name)
        func = getattr(record, "funcName", "")
        lineno = getattr(record, "lineno", "")
        method = getattr(record, "http_method", "")
        route = getattr(record, "http_route", "")
        status = getattr(record, "http_status", "")
        client = getattr(record, "client_host", "")
        duration = getattr(record, "duration_ms", "")
        exc = record.exc_info

        entry = f"{module}.py"
        if func:
            entry = f"{module}/{func}.py"
        if lineno:
            entry += f":{lineno}"

        parts = [f"[{ts}] [{level}]"]

        if method and route:
            parts.append(f"[{method}]")
        parts.append(f"[{entry}]")
        if route:
            parts.append(f"[{route}]")
        if status:
            parts.append(f"[{status}]")
        if duration:
            parts.append(f"[{duration}ms]")
        if client:
            parts.append(f"[{client}]")

        parts.append(record.getMessage())
        line = " ".join(parts)

        if exc and exc[0] is not None:
            line += "\n" + self.formatException(exc)

        return line


def setup_logging(log_dir: str = "logs", level: int = logging.DEBUG) -> None:
    """Configure root 'videasy' logger with console + file handlers."""
    root = logging.getLogger("videasy")
    root.setLevel(level)
    root.propagate = False

    if root.handlers:
        return

    # ── Console handler (colored) ──
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(RequestFormatter(use_colors=True))
    root.addHandler(console)

    # ── File handler (plain text, all levels) ──
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path / "app.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(FileFormatter())
    root.addHandler(fh)

    # ── Suppress noisy libraries ──
    for name in ("httpx", "httpcore", "hpack", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)

    root.info("logging initialized — console + %s/app.log", log_dir)


# ─── Request Logging Middleware ───────────────────────────────────────


class RequestLoggingMiddleware:
    """ASGI middleware that logs every HTTP request with:
    method, route, status code, duration, client IP."""

    SKIP_PATHS = frozenset({"/health", "/favicon.ico"})

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path: str = scope.get("path", "")
        method: str = scope.get("method", "?")
        client = scope.get("client")
        client_host = client[0] if client else "?"

        # Skip noisy health checks
        if path in self.SKIP_PATHS:
            return await self.app(scope, receive, send)

        start = time.perf_counter()
        status_code = 0

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            status_code = 499
        except Exception:
            status_code = 500
            raise
        finally:
            elapsed = round((time.perf_counter() - start) * 1000, 1)

            if status_code == 499:
                level = logging.DEBUG
            elif status_code >= 500:
                level = logging.ERROR
            elif status_code >= 400:
                level = logging.WARNING
            else:
                level = logging.INFO

            log_record = logging.LogRecord(
                name="videasy",
                level=level,
                pathname="",
                lineno=0,
                msg="",
                args=(),
                exc_info=None,
            )
            # Inject extra fields for the formatter
            log_record.module = "request"
            log_record.funcName = ""
            log_record.http_method = method
            log_record.http_route = path
            log_record.http_status = str(status_code)
            log_record.duration_ms = str(elapsed)
            log_record.client_host = client_host

            effective_level = logging.INFO if status_code < 400 else logging.WARNING
            logger.handle(log_record)
