from __future__ import annotations

from typing import Any


def classify_issue(error: Any) -> str:
    text = str(error or "").strip().lower()
    if not text:
        return "unknown"

    if "database is locked" in text or "sqlite" in text and "locked" in text:
        return "db_locked"

    if "timed out" in text or "timeout" in text:
        if "session_timeout" in text or "finish:timeout" in text:
            return "session_timeout"
        return "network_timeout"

    if "connection refused" in text or "name or service not known" in text or "failed to establish a new connection" in text:
        return "network_connection"

    if "http 5" in text:
        return "http_5xx"

    if "http 4" in text:
        if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
            return "auth"
        if "404" in text or "node_not_registered" in text or "session_not_found" in text:
            return "not_found"
        return "http_4xx"

    if "node_not_registered" in text or "session_not_found" in text:
        return "not_found"

    if "cluster key" in text or "api key" in text or "unauthorized" in text or "forbidden" in text or "认证" in text:
        return "auth"

    if "quota" in text:
        return "quota"

    return "unknown"


def diag_label(error: Any) -> str:
    return f"diag={classify_issue(error)}"
