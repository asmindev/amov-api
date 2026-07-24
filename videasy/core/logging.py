from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send


logger = logging.getLogger("videasy")


class RequestFormatter(logging.Formatter):
    """Colored console formatter.
    Request:  [ts] [LEVEL] "GET /path?q=1 HTTP/1.1" 200 23.0ms
    App log:  [ts] [LEVEL] [module.py:42] message
    """

    LEVEL_COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    STATUS_COLORS = {
        2: "\033[32m",  # green
        3: "\033[36m",  # cyan
        4: "\033[33m",  # yellow
        5: "\033[31m",  # red
    }
    RESET = "\033[0m"

    def __init__(self, use_colors: bool = True) -> None:
        super().__init__()
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f")[:-3]
        level = record.levelname
        method = getattr(record, "http_method", "")
        route = getattr(record, "http_route", "")
        status = getattr(record, "http_status", "")
        duration = getattr(record, "duration_ms", "")

        if self.use_colors:
            level_str = f"{self.LEVEL_COLORS.get(record.levelno, '')}{level}{self.RESET}"
        else:
            level_str = level

        if method and route:
            status_word = _status_word(status)
            if self.use_colors and status:
                code = int(status)
                sc = self.STATUS_COLORS.get(code // 100, "")
                status_str = f"{sc}{status}{self.RESET}"
            else:
                status_str = status
            return (
                f"[{ts}] [{level_str}] "
                f'"{method} {route} HTTP/1.1" {status_str} {status_word} {duration}ms'
            )

        # App log: [module/func.py:line] msg
        module = getattr(record, "module", record.name)
        func = getattr(record, "funcName", "")
        lineno = getattr(record, "lineno", "")
        entry = f"{module}.py"
        if func:
            entry = f"{module}/{func}.py"
        if lineno:
            entry += f":{lineno}"

        msg = record.getMessage()
        return f"[{ts}] [{level_str}] {entry} {msg}"


class FileFormatter(logging.Formatter):
    """Plain text file formatter (no colors).
    Request:  [ts] [LEVEL] "GET /path?q=1 HTTP/1.1" 200 OK 23.0ms
    App log:  [ts] [LEVEL] [module.py:42] message
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f")[:-3]
        level = record.levelname
        method = getattr(record, "http_method", "")
        route = getattr(record, "http_route", "")
        status = getattr(record, "http_status", "")
        duration = getattr(record, "duration_ms", "")

        if method and route:
            status_word = _status_word(status)
            line = (
                f"[{ts}] [{level}] "
                f'"{method} {route} HTTP/1.1" {status} {status_word} {duration}ms'
            )
        else:
            module = getattr(record, "module", record.name)
            func = getattr(record, "funcName", "")
            lineno = getattr(record, "lineno", "")
            entry = f"{module}.py"
            if func:
                entry = f"{module}/{func}.py"
            if lineno:
                entry += f":{lineno}"

            msg = record.getMessage()
            line = f"[{ts}] [{level}] {entry} {msg}"

        if record.exc_info and record.exc_info[0] is not None:
            line += "\n" + self.formatException(record.exc_info)

        return line


def _status_word(code: str) -> str:
    words = {
        "200": "OK",
        "201": "Created",
        "204": "No Content",
        "301": "Moved Permanently",
        "302": "Found",
        "304": "Not Modified",
        "400": "Bad Request",
        "401": "Unauthorized",
        "403": "Forbidden",
        "404": "Not Found",
        "405": "Method Not Allowed",
        "408": "Request Timeout",
        "429": "Too Many Requests",
        "499": "Client Closed",
        "500": "Internal Server Error",
        "502": "Bad Gateway",
        "503": "Service Unavailable",
        "504": "Gateway Timeout",
    }
    return words.get(code, "")


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
    for name in ("httpx", "httpcore", "hpack", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).setLevel(logging.WARNING)

    root.info("logging initialized — console + %s/app.log", log_dir)


# ─── Request Logging Middleware ───────────────────────────────────────


class RequestLoggingMiddleware:
    """ASGI middleware that logs every HTTP request:
    "METHOD /path?query HTTP/1.1" STATUS DURATIONms"""

    SKIP_PATHS = frozenset({"/health", "/favicon.ico"})

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path: str = scope.get("path", "")
        method: str = scope.get("method", "?")
        query_string: bytes = scope.get("query_string", b"")
        client = scope.get("client")
        client_host = client[0] if client else "?"

        if path in self.SKIP_PATHS:
            return await self.app(scope, receive, send)

        full_route = path
        if query_string:
            full_route += "?" + query_string.decode("utf-8", errors="replace")

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

            rec = logging.LogRecord(
                name="videasy",
                level=level,
                pathname="",
                lineno=0,
                msg="",
                args=(),
                exc_info=None,
            )
            rec.module = "request"
            rec.funcName = ""
            rec.http_method = method
            rec.http_route = full_route
            rec.http_status = str(status_code)
            rec.duration_ms = str(elapsed)
            rec.client_host = client_host

            logger.handle(rec)
