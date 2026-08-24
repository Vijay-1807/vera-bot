"""Vera challenge HTTP API using only the Python standard library."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from vera import config
from vera.engine import Engine


logging.basicConfig(level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
LOG = logging.getLogger("vera")
ENGINE = Engine()


class Handler(BaseHTTPRequestHandler):
    server_version = "Vera/1.0"

    def do_HEAD(self):
        path = urlparse(self.path).path.rstrip("/")
        status = 200 if path in {"", "/v1/healthz", "/v1/metadata"} else 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "":
            self.respond(200, {
                "name": "Vera Signal Foundry",
                "status": "ready",
                "healthz": "/v1/healthz",
                "metadata": "/v1/metadata",
            })
        elif path == "/v1/healthz":
            self.respond(200, ENGINE.store.health())
        elif path == "/v1/metadata":
            self.respond(200, config.metadata())
        elif path == "/v1/diagnostics":
            self.respond(200, ENGINE.store.diagnostics())
        else:
            self.respond(404, {"error": "not_found"})

    def do_POST(self):
        try:
            payload = self.read_json()
        except ValueError as exc:
            self.respond(400, {"accepted": False, "reason": "invalid_json", "details": str(exc)})
            return
        path = urlparse(self.path).path.rstrip("/")
        try:
            if path == "/v1/context":
                self.push_context(payload)
            elif path == "/v1/tick":
                self.respond(200, ENGINE.tick(payload))
            elif path == "/v1/reply":
                self.reply(payload)
            elif path == "/v1/teardown":
                self.respond(200, ENGINE.store.teardown())
            else:
                self.respond(404, {"error": "not_found"})
        except Exception:
            LOG.exception("request failed: %s", path)
            self.respond(500, {"error": "internal_error"})

    def push_context(self, payload):
        scope = payload.get("scope")
        if scope not in ENGINE.store.contexts:
            self.respond(400, {"accepted": False, "reason": "invalid_scope"})
            return
        context_id = payload.get("context_id")
        version = payload.get("version")
        body = payload.get("payload")
        if not isinstance(context_id, str) or not context_id or not isinstance(version, int) or version < 1:
            self.respond(400, {"accepted": False, "reason": "invalid_context_envelope"})
            return
        if not isinstance(body, dict):
            self.respond(400, {"accepted": False, "reason": "invalid_payload"})
            return
        status, response = ENGINE.store.put_context(
            scope, context_id, version, body, payload.get("delivered_at"))
        self.respond(status, response)

    def reply(self, payload):
        if not payload.get("conversation_id") or not isinstance(payload.get("message"), str):
            self.respond(400, {"error": "invalid_reply"})
            return
        self.respond(200, ENGINE.reply(payload))

    def read_json(self):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > config.MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)


def main():
    warnings = config.identity_warnings()
    for warning in warnings:
        LOG.warning(warning)
    server = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    LOG.info("Vera listening on %s:%s", config.HOST, config.PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
