"""Small Render relay for one ESP32 and one authorized owner."""
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import json
import os
import re
import secrets
from threading import Lock
import time


PANEL_ORIGIN = "https://nexus-home-assistant.onrender.com"
ONLINE_WINDOW = 20
COMMAND_TTL = 30
RESULT_WINDOW = 180
MAX_PENDING = 8
MAX_COMMANDS = 128
MAX_DEVICE_BODY = 8192
MAX_OWNER_BODY = 2048
DEVICE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)
COMMAND_ID = re.compile(r"[A-Za-z0-9_-]{24}", re.ASCII)
TOKEN = re.compile(r"Bearer ([A-Za-z0-9_-]{32,128})", re.ASCII)
HASH = re.compile(r"[A-Fa-f0-9]{64}", re.ASCII)


class RelayError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def configured_hash(environ, name):
    value = environ.get(name, "")
    if not HASH.fullmatch(value):
        raise ValueError(name + " must be a 64-character SHA-256 hex digest")
    return bytes.fromhex(value)


def panel_origin(value):
    try:
        url = urlsplit(value)
        host = url.hostname
        url.port
    except ValueError as exc:
        raise ValueError("KONTUR_PANEL_ORIGIN must be an exact origin") from exc
    if (url.scheme != "https" and not (url.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"))
            or not host or url.username or url.password or value != f"{url.scheme}://{url.netloc}"):
        raise ValueError("KONTUR_PANEL_ORIGIN must be an exact origin")
    return value


@dataclass(frozen=True)
class Config:
    owner_hash: bytes
    device_hash: bytes
    device_id: str
    origin: str
    host: str
    port: int

    @classmethod
    def from_env(cls, environ=os.environ):
        owner_hash = configured_hash(environ, "KONTUR_OWNER_SHA256")
        device_hash = configured_hash(environ, "KONTUR_DEVICE_SHA256")
        if compare_digest(owner_hash, device_hash):
            raise ValueError("Owner and device credentials must be different")
        device_id = environ.get("KONTUR_DEVICE_ID", "")
        if not DEVICE_ID.fullmatch(device_id):
            raise ValueError("KONTUR_DEVICE_ID is missing or invalid")
        origin = panel_origin(environ.get("KONTUR_PANEL_ORIGIN", PANEL_ORIGIN))
        on_render = environ.get("RENDER") == "true" or bool(environ.get("RENDER_SERVICE_ID"))
        host = environ.get("KONTUR_CLOUD_HOST", "0.0.0.0" if on_render else "127.0.0.1")
        try:
            port = int(environ.get("PORT", "10000"))
        except ValueError as exc:
            raise ValueError("PORT must be a numeric TCP port") from exc
        if not host or not 1 <= port <= 65535:
            raise ValueError("Relay host or port is invalid")
        return cls(owner_hash, device_hash, device_id, origin, host, port)


def authenticate(header, expected):
    match = TOKEN.fullmatch(header or "")
    return bool(match and compare_digest(sha256(match[1].encode("ascii")).digest(), expected))


def label(value):
    return (isinstance(value, str) and 0 < len(value) <= 60 and value.strip()
            and not any(ord(char) < 32 or 0xd800 <= ord(char) <= 0xdfff for char in value))


def normalize_manifest(raw, device_id):
    if not isinstance(raw, dict) or raw.get("id") != device_id:
        raise RelayError(400, "invalid-device-manifest")
    caps = raw.get("capabilities")
    if (type(raw.get("schemaVersion")) is not int or raw["schemaVersion"] != 1
            or raw.get("kind") != "light" or not label(raw.get("name"))
            or not isinstance(caps, list) or len(caps) != 1):
        raise RelayError(400, "invalid-device-manifest")
    power = caps[0]
    if (not isinstance(power, dict) or power.get("id") != "power" or power.get("type") != "switch"
            or power.get("momentary") is not True or not label(power.get("label"))):
        raise RelayError(400, "invalid-device-manifest")
    return {
        "schemaVersion": 1, "id": device_id, "kind": "light", "name": raw["name"].strip(),
        "capabilities": [
            {"id": "power", "type": "switch", "label": power["label"].strip(), "momentary": True}
        ]
    }


def normalize_results(raw):
    if not isinstance(raw, list) or len(raw) > 16:
        raise RelayError(400, "invalid-device-results")
    results = {}
    for row in raw:
        if not isinstance(row, dict):
            raise RelayError(400, "invalid-device-results")
        command_id, ok = row.get("id"), row.get("ok")
        if (not isinstance(command_id, str) or not COMMAND_ID.fullmatch(command_id)
                or type(ok) is not bool or command_id in results):
            raise RelayError(400, "invalid-device-results")
        results[command_id] = ok
    return results


@dataclass
class Command:
    created: float
    status: str = "pending"
    terminal_at: float = 0
    delivered: bool = False

    def finish(self, status, now):
        self.status = status
        self.terminal_at = now


class Relay:
    def __init__(self, device_id, clock=time.monotonic):
        self.device_id = device_id
        self.clock = clock
        self.lock = Lock()
        self.manifest = None
        self.last_seen = None
        self.commands = {}

    def online(self, now):
        return self.last_seen is not None and now - self.last_seen < ONLINE_WINDOW

    def maintain(self, now):
        for command_id, command in list(self.commands.items()):
            if command.status == "pending" and now >= command.created + COMMAND_TTL:
                command.finish("expired", command.created + COMMAND_TTL)
            if command.status != "pending" and now >= command.terminal_at + RESULT_WINDOW:
                del self.commands[command_id]

    def devices(self):
        with self.lock:
            devices = []
            if self.manifest is not None:
                devices.append({"manifest": self.manifest, "online": self.online(self.clock())})
            return {"devices": devices}

    def enqueue(self, body):
        if body.get("deviceId") != self.device_id:
            raise RelayError(404, "device-not-found")
        if body.get("capability") != "power" or body.get("value") is not True:
            raise RelayError(400, "invalid-command")
        with self.lock:
            now = self.clock()
            self.maintain(now)
            if not self.online(now):
                raise RelayError(409, "device-offline")
            if sum(command.status == "pending" for command in self.commands.values()) >= MAX_PENDING:
                raise RelayError(429, "command-queue-full")
            if len(self.commands) >= MAX_COMMANDS:
                oldest = next(key for key, command in self.commands.items() if command.status != "pending")
                del self.commands[oldest]
            command_id = secrets.token_urlsafe(18)
            self.commands[command_id] = Command(now)
            return {"id": command_id}

    def status(self, command_id):
        with self.lock:
            self.maintain(self.clock())
            command = self.commands.get(command_id)
            if command is None:
                raise RelayError(404, "command-not-found")
            return {"status": command.status}

    def poll(self, body):
        manifest = normalize_manifest(body.get("manifest"), self.device_id)
        results = normalize_results(body.get("results"))
        with self.lock:
            now = self.clock()
            self.maintain(now)
            self.manifest = manifest
            self.last_seen = now
            for key, ok in results.items():
                command = self.commands.get(key)
                if command is not None and command.status == "pending" and command.delivered:
                    command.finish("applied" if ok else "failed", now)
            for key, command in self.commands.items():
                if command.status == "pending":
                    command.delivered = True
                    return {"command": {"id": key, "capability": "power", "value": True}}
            return {"command": None}


def owner_method(path):
    if path == "/api/cloud/devices":
        return "GET"
    if path == "/api/cloud/commands":
        return "POST"
    command_prefix = "/api/cloud/commands/"
    if path.startswith(command_prefix) and COMMAND_ID.fullmatch(path[len(command_prefix):]):
        return "GET"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "KonturRelay/1"
    sys_version = ""

    def log_request(self, code="-", size="-"):
        self.log_message("%s %s", self.command, code)

    def log_error(self, *_):
        self.log_message("Invalid or incomplete connection")

    def send_json(self, status, body, origin=None):
        payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(payload)

    def path_only(self):
        try:
            url = urlsplit(self.path)
        except ValueError:
            return ""
        if url.query or url.fragment or url.scheme or url.netloc:
            return ""
        return url.path

    def read_json(self, limit):
        sizes = self.headers.get_all("Content-Length") or []
        if len(sizes) != 1 or self.headers.get("Transfer-Encoding") or not re.fullmatch("[0-9]{1,8}", sizes[0]):
            raise RelayError(400, "invalid-request")
        size = int(sizes[0])
        if size > limit:
            raise RelayError(413, "request-too-large")
        if not size or self.headers.get_content_type() != "application/json":
            raise RelayError(400, "invalid-request")
        try:
            raw = self.rfile.read(size)
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            raise RelayError(400, "invalid-request") from exc
        if len(raw) != size or not isinstance(body, dict):
            raise RelayError(400, "invalid-request")
        return body

    def run_route(self):
        path = self.path_only()
        config = self.server.config
        relay = self.server.relay
        origins = self.headers.get_all("Origin", [])
        origin = self.headers.get("Origin")
        cors = None
        try:
            if path == "/healthz" and self.command == "GET":
                return self.send_json(200, {"ok": True})
            if path == "/api/cloud/device/poll" and self.command == "POST":
                if origin is not None or not authenticate(self.headers.get("Authorization"), config.device_hash):
                    raise RelayError(401, "device-unauthorized")
                return self.send_json(200, relay.poll(self.read_json(MAX_DEVICE_BODY)))
            if owner_method(path) != self.command:
                raise RelayError(404, "not-found")
            if origins and origins != [config.origin]:
                raise RelayError(403, "wrong-origin")
            cors = origin
            if not authenticate(self.headers.get("Authorization"), config.owner_hash):
                raise RelayError(401, "owner-unauthorized")
            if path == "/api/cloud/devices":
                return self.send_json(200, relay.devices(), cors)
            if path == "/api/cloud/commands":
                return self.send_json(202, relay.enqueue(self.read_json(MAX_OWNER_BODY)), cors)
            return self.send_json(200, relay.status(path.rsplit("/", 1)[1]), cors)
        except RelayError as exc:
            return self.send_json(exc.status, {"error": exc.code}, cors)

    do_GET = run_route
    do_POST = run_route

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        expected = owner_method(self.path_only())
        headers = {part.strip().lower() for part in self.headers.get("Access-Control-Request-Headers", "").split(",")}
        headers.discard("")
        if (self.headers.get_all("Origin", []) != [self.server.config.origin] or expected is None
                or self.headers.get("Access-Control-Request-Method") != expected
                or not headers <= {"authorization", "content-type"}):
            return self.send_json(403, {"error": "wrong-origin"})
        self.send_response(204)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", expected)
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Vary", "Origin, Access-Control-Request-Method, Access-Control-Request-Headers")
        self.end_headers()


class Server(ThreadingHTTPServer):
    def __init__(self, address, config):
        self.config = config
        self.relay = Relay(config.device_id)
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(8)
        return connection, address


def run():
    try:
        config = Config.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    with Server((config.host, config.port), config) as server:
        print("Kontur cloud relay ready", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    run()
