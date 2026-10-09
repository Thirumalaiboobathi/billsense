"""Lambda Function URL handler: GET / serves the UI, POST /ask runs the agent, POST /feedback logs a 👍/👎."""
import base64
import hmac
import json
import os
import re
import time
from pathlib import Path

import agent

INDEX_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
FRIENDLY_ERROR = "Something went wrong on my side. Please try again in a moment."

SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "connect-src 'self'; frame-ancestors 'none'"
    ),
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}

CREDENTIAL_MSG = ("That looks like an AWS access key. I didn't send it anywhere. "
                  "If it's real, deactivate and rotate it in IAM now, and never paste keys into web apps.")

# Credential guard: AWS access key IDs and 40-char secret-like strings
_KEY_ID_RE = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")
_SECRET_RE = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Best-effort limits (per warm container, per source IP)
MAX_FAILS, WINDOW_SECONDS = 5, 600       # wrong passcodes
MAX_ASKS = 20                            # all /ask requests, demo included
CONTEXT_TURNS, CONTEXT_CHARS = 2, 1500
_fails: dict = {}
_asks: dict = {}


def looks_like_credential(*texts: str) -> bool:
    for t in texts:
        if not t:
            continue
        if _KEY_ID_RE.search(t):
            return True
        for m in _SECRET_RE.finditer(t):
            s = m.group(0)
            if re.search(r"\d", s) and re.search(r"[A-Za-z]", s):
                return True
    return False


def _resp(status: int, body, content_type: str = "application/json"):
    if content_type == "application/json":
        body = json.dumps(body, ensure_ascii=False)
    headers = {"content-type": f"{content_type}; charset=utf-8", **SECURITY_HEADERS}
    return {"statusCode": status, "headers": headers, "body": body}


def _key_ok(given: str) -> bool:
    expected = os.environ.get("ACCESS_KEY", "")
    if not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _retry_after(bucket: dict, ip: str) -> int:
    """Seconds until the oldest hit in the window expires (0 if not limited)."""
    now = time.time()
    recent = [t for t in bucket.get(ip, []) if now - t < WINDOW_SECONDS]
    bucket[ip] = recent
    return int(recent[0] + WINDOW_SECONDS - now) + 1 if recent else 0


def _too_many(retry_after: int):
    mins = max(1, -(-retry_after // 60))
    return _resp(429, {"error": "too_many_attempts", "code": "RATE_LIMITED", "retry_after": retry_after,
                       "message": f"Too many attempts, try again in {mins} min."})


def _clean_text(value, limit: int) -> str:
    return _CTRL_RE.sub("", str(value or "")).strip()[:limit]


def _history(raw) -> list:
    """Last 2 turns, each field capped. Server-side enforcement of what the client promises."""
    out = []
    if isinstance(raw, list):
        for t in raw[-CONTEXT_TURNS:]:
            if isinstance(t, dict):
                out.append({"q": _clean_text(t.get("q"), CONTEXT_CHARS), "a": _clean_text(t.get("a"), CONTEXT_CHARS)})
    return out


def _log(request_id, mode, verdict, started, tools_used):
    print(json.dumps({"request_id": request_id, "mode": mode, "verdict": verdict,
                      "latency_ms": int((time.time() - started) * 1000), "tools_used": tools_used}))


def _body(event) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


def handler(event, context):
    started = time.time()
    request_id = getattr(context, "aws_request_id", "local")
    try:
        http = event.get("requestContext", {}).get("http", {})
        method, path = http.get("method", "GET"), event.get("rawPath", "/")
        ip = http.get("sourceIp", "unknown")
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

        if method == "GET" and path == "/":
            return _resp(200, INDEX_HTML, "text/html")

        if method == "POST" and path == "/feedback":
            data = _body(event)
            verdict, value, mode = data.get("verdict"), data.get("value"), data.get("mode")
            if verdict not in ("green", "amber", "red") or value not in ("up", "down") or mode not in ("demo", "owner"):
                return _resp(400, {"error": "bad_feedback", "message": "Invalid feedback."})
            print(json.dumps({"event": "feedback", "verdict": verdict, "value": value, "mode": mode}))
            return _resp(200, {"ok": True})

        if method == "POST" and path == "/ask":
            # Per-IP limit on every ask (demo included); counted before any work is done
            wait = _retry_after(_asks, ip)
            if len(_asks.get(ip, [])) >= MAX_ASKS:
                return _too_many(wait)
            _asks.setdefault(ip, []).append(time.time())

            data = _body(event)
            question = _clean_text(data.get("question"), 500)
            history = _history(data.get("context"))
            demo = data.get("demo") is True
            passcode = headers.get("x-billsense-key", "")

            # Block before anything is logged or sent to the model (question, context and passcode)
            ctx_text = [x for h in history for x in (h["q"], h["a"])]
            if looks_like_credential(question, passcode, *ctx_text):
                return _resp(400, {"error": "CREDENTIAL_DETECTED", "code": "CREDENTIAL_DETECTED", "message": CREDENTIAL_MSG})
            if not question:
                return _resp(400, {"error": "empty_question", "code": "EMPTY_QUESTION", "message": "Ask me something about your bill."})
            if not demo:
                wait = _retry_after(_fails, ip)
                if len(_fails.get(ip, [])) >= MAX_FAILS:
                    return _too_many(wait)
                if not _key_ok(passcode):
                    if passcode:  # an absent passcode is not a guess
                        _fails.setdefault(ip, []).append(time.time())
                    return _resp(401, {"error": "bad_key", "code": "BAD_PASSCODE", "message": "That owner passcode didn't work."})
            try:
                result = agent.ask(question, demo=demo, history=history)
            except agent.AskError as e:
                status = {"COST_ACCESS": 422, "BEDROCK_THROTTLED": 503}.get(e.code, 500)
                msgs = {"COST_ACCESS": "Cost Explorer isn't available for this account yet.",
                        "BEDROCK_THROTTLED": "I'm a bit busy right now."}
                _log(request_id, "demo" if demo else "owner", e.code, started, [])
                return _resp(status, {"error": e.code, "code": e.code, "message": msgs.get(e.code, FRIENDLY_ERROR)})
            _log(request_id, "demo" if demo else "owner", result["verdict"], started, result["tools_used"])
            return _resp(200, result)

        return _resp(404, {"error": "not_found", "message": "Nothing here."})
    except Exception as e:  # never leak details to the client; never log request content
        print(json.dumps({"request_id": request_id, "error": type(e).__name__}))
        return _resp(500, {"error": "server_error", "code": "SERVER_ERROR", "message": FRIENDLY_ERROR})
