"""Bounded control calls to the local native editor, without mutation retries.

Call this synchronous client from a background owner. A lost reply does not mean
an edit failed: the caller must inspect the native document before deciding what
to do next. Each call uses a fresh connection, so a late reply cannot answer a
different request. This transport owns no timeline, history, or project model.
"""
from __future__ import annotations

import json
import math
import socket
import time
import uuid


MAX_REQUEST = 4 * 1024 * 1024
MAX_REPLY = 32 * 1024 * 1024


class BridgeError(RuntimeError):
    pass


class EditorUnavailable(BridgeError):
    """No connection was established; no command was sent by this call."""


class OutcomeUnknown(BridgeError):
    """The request may have executed. Reconcile state; do not repeat it blindly."""

    def __init__(self, request_id: str, method: str, reason: str):
        self.request_id = request_id
        self.method = method
        super().__init__(f"Native outcome is unknown for {method} ({request_id}): {reason}")


class NativeCommandError(BridgeError):
    """The editor returned an error. This is not a claim of zero side effects."""

    def __init__(self, request_id: str, method: str, reason: str):
        self.request_id = request_id
        self.method = method
        self.reason = reason
        super().__init__(reason)


def _finite_json(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Native request object keys must be strings")
        for item in value.values():
            _finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _finite_json(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("Native requests require JSON values")
    elif type(value) is float and not math.isfinite(value):
        raise ValueError("Native requests require finite JSON values")


class FilmcraftBridge:
    def __init__(self, port: int, *, timeout: float = 10.0, max_reply: int = MAX_REPLY):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("A valid native editor port is required")
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("The native call deadline must be finite and within 60 seconds")
        if type(max_reply) is not int or not 1 <= max_reply <= MAX_REPLY:
            raise ValueError("The native reply limit is outside the supported range")
        self.port = port
        self.timeout = timeout
        self.max_reply = max_reply

    def call(self, method: str, params: dict | None = None):
        """Send exactly once and return the native result, with a total deadline."""
        if not isinstance(method, str) or not method or len(method) > 256:
            raise ValueError("A bounded native method name is required")
        params = {} if params is None else params
        if not isinstance(params, dict):
            raise ValueError("Native parameters must be an object")
        if method in ("engine.execute", "ui.menu.invoke"):
            command = params.get("command")
            if not isinstance(command, str):
                command = params.get("id")
            arguments = params.get("params", {})
            if command == "file.exportMedia" and isinstance(arguments, dict) and arguments.get("wait") is True:
                raise ValueError("Start exports with wait=false, then inspect jobs.list")
        request_id = str(uuid.uuid4())
        try:
            _finite_json(params)
            payload = json.dumps({"id": request_id, "method": method, "params": params},
                                 ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, RecursionError, UnicodeError) as exc:
            raise ValueError("Native parameters must be finite UTF-8 JSON data") from exc
        if len(payload) > MAX_REQUEST:
            raise ValueError("The native request exceeds the 4 MiB protocol limit")
        deadline = time.monotonic() + self.timeout
        try:
            connection = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        except OSError as exc:
            raise EditorUnavailable("Cannot connect to the local native editor") from exc
        with connection:
            try:
                self._remaining(connection, deadline)
                connection.sendall(payload + b"\n")
                received = bytearray()
                while True:
                    self._remaining(connection, deadline)
                    block = connection.recv(min(65536, self.max_reply + 1 - len(received)))
                    if not block:
                        raise ValueError("The editor closed before a complete reply")
                    received.extend(block)
                    newline = received.find(b"\n")
                    if newline >= 0:
                        if newline > self.max_reply:
                            raise ValueError("The native reply exceeds its size limit")
                        reply = json.loads(received[:newline], parse_constant=self._reject_constant)
                        _finite_json(reply)
                        break
                    if len(received) > self.max_reply:
                        raise ValueError("The native reply exceeds its size limit")
                if not isinstance(reply, dict) or reply.get("id") != request_id or type(reply.get("ok")) is not bool:
                    raise ValueError("The native reply does not match this request")
                if reply["ok"]:
                    if "result" not in reply:
                        raise ValueError("The native result is absent")
                    return reply["result"]
                reason = reply.get("error")
                if not isinstance(reason, str):
                    raise ValueError("The native error is malformed")
                # The server's UI wait can expire while the queued edit still runs.
                if reason == "timeout":
                    raise ValueError("The native server stopped waiting for the command")
                raise NativeCommandError(request_id, method, reason)
            except NativeCommandError:
                raise
            except (OSError, ValueError, RecursionError) as exc:
                raise OutcomeUnknown(request_id, method, str(exc)) from exc

    @staticmethod
    def _remaining(connection, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The native call deadline expired")
        connection.settimeout(remaining)

    @staticmethod
    def _reject_constant(value):
        raise ValueError(f"Non-finite native JSON value: {value}")

    def execute(self, command: str, params: dict | None = None):
        """Dispatch through Session.execute; the native engine owns all edits."""
        if not isinstance(command, str) or not command or len(command) > 256:
            raise ValueError("A bounded native command ID is required")
        params = {} if params is None else params
        if not isinstance(params, dict):
            raise ValueError("Native command parameters must be an object")
        return self.call("engine.execute", {"command": command, "params": params})
