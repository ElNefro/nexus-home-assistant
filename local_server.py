"""Loopback-only bridge between the Kontur dashboard and DNS-SD devices."""
import http.client
import ipaddress
import json
import math
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf


SERVICE = "_kontur._tcp.local."
PUBLIC = Path(__file__).resolve().parent / "public"
HOST = "127.0.0.1:8080"
ORIGIN = f"http://{HOST}"
DEVICE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}")
LOCAL_NETWORKS = tuple(map(ipaddress.IPv4Network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"
)))
LIMIT = 32_768


def finite(value):
    return type(value) in (int, float) and abs(value) <= 1e100 and math.isfinite(value)


def normalize_manifest(value):
    if not isinstance(value, dict):
        raise ValueError("manifest")
    device_id, name, kind = (value.get(key) for key in ("id", "name", "kind"))
    capabilities = value.get("capabilities")
    if (type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1
            or not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id)
            or not isinstance(name, str) or not name.strip() or len(name) > 60
            or not isinstance(kind, str) or not CAPABILITY_ID.fullmatch(kind)
            or not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 20):
        raise ValueError("manifest")
    seen = set()
    for entry in capabilities:
        if not isinstance(entry, dict):
            raise ValueError("capability")
        cap_id, label, cap_type = (entry.get(key) for key in ("id", "label", "type"))
        if (not isinstance(cap_id, str) or not CAPABILITY_ID.fullmatch(cap_id) or cap_id in seen
                or not isinstance(label, str) or not label.strip() or len(label) > 60
                or cap_type not in ("switch", "slider", "sensor")):
            raise ValueError("capability")
        seen.add(cap_id)
        if any(not isinstance(entry.get(key, ""), str) or len(entry.get(key, "")) > limit
               for key, limit in (("unit", 12), ("description", 500))):
            raise ValueError("capability")
        if cap_type == "slider":
            lo, hi, step = (entry.get(key) for key in ("min", "max", "step"))
            if not all(map(finite, (lo, hi, step))) or hi <= lo or not 0 < step <= hi - lo:
                raise ValueError("capability")
    return {"schemaVersion": 1, "id": device_id, "name": name.strip(), "kind": kind,
            "capabilities": capabilities, "requiresToken": value.get("requiresToken") is not False}


def state_for(manifest, raw):
    if not isinstance(raw, dict):
        raise ValueError("state")
    return {cap["id"]: raw[cap["id"]] for cap in manifest["capabilities"]
            if cap["id"] in raw and valid_value(cap, raw[cap["id"]])}


def valid_value(cap, value):
    if cap["type"] == "switch":
        return type(value) is bool
    if not finite(value):
        return False
    if cap["type"] == "slider":
        return cap["min"] <= value <= cap["max"]
    return True


def device_request(address, route, command=None, token=None):
    headers = {"Accept": "application/json"}
    body = None
    method = "GET"
    if command is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
        body = json.dumps(command, allow_nan=False).encode()
    if token:
        headers["Authorization"] = "Bearer " + token
    connection = http.client.HTTPConnection(*address, timeout=1.6)
    try:
        connection.request(method, "/kontur/v1/" + route, body=body, headers=headers)
        response = connection.getresponse()
        if response.status == 401 or response.status == 403:
            raise PermissionError("device-token")
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            raise ValueError("device-response")
        payload = response.read(LIMIT + 1)
        if len(payload) > LIMIT:
            raise ValueError("device-response")
        return json.loads(payload)
    finally:
        connection.close()


class Devices(ServiceListener):
    def __init__(self):
        self.records = {}

    def add_service(self, zeroconf, service_type, name):
        self.update_service(zeroconf, service_type, name)

    def update_service(self, zeroconf, service_type, name):
        info = zeroconf.get_service_info(service_type, name, 450)
        if info is not None:
            self.records[name] = info

    def remove_service(self, zeroconf, service_type, name):
        self.records.pop(name, None)


def resolve_record(info):
    properties = info.properties
    advertised_id = (properties.get(b"id") or b"").decode("ascii", errors="ignore")
    kind = (properties.get(b"kind") or b"").decode("ascii", errors="ignore")
    for host in info.parsed_addresses(IPVersion.V4Only):
        ip = ipaddress.IPv4Address(host)
        if not any(ip in network for network in LOCAL_NETWORKS) or not 1 <= info.port <= 65535:
            continue
        address = (host, info.port)
        try:
            manifest = normalize_manifest(device_request(address, "manifest"))
            if advertised_id and advertised_id != manifest["id"]:
                continue
            return (manifest, address)
        except (OSError, ValueError, http.client.HTTPException):
            continue
    if (properties.get(b"v") == b"1" and re.fullmatch(r"[0-9a-f]{12}", advertised_id)
            and CAPABILITY_ID.fullmatch(kind)):
        return ({"id": advertised_id, "kind": kind, "legacy": True}, None)
    return None


def discover():
    listener = Devices()
    with Zeroconf(ip_version=IPVersion.V4Only) as zeroconf:
        browser = ServiceBrowser(zeroconf, SERVICE, listener)
        try:
            time.sleep(1.6)
        finally:
            browser.cancel()
        records = list(listener.records.values())[:20]
    with ThreadPoolExecutor(max_workers=10) as pool:
        return [entry for entry in pool.map(resolve_record, records) if entry]


class Registry:
    def __init__(self):
        self.discovered = {}
        self.sessions = {}
        self.lock = Lock()
        self.scan_lock = Lock()

    def scan(self):
        with self.scan_lock:
            found = {}
            duplicates = set()
            for manifest, address in discover():
                device_id = manifest["id"]
                if device_id in found and found[device_id][1] != address:
                    duplicates.add(device_id)
                found[device_id] = (manifest, address)
            for device_id in duplicates:
                del found[device_id]
            with self.lock:
                self.discovered = found
            return [found[key][0] for key in sorted(found)]

    def pair(self, device_id, token):
        with self.lock:
            manifest, address = self.discovered[device_id]
        if manifest.get("legacy"):
            raise ValueError("legacy")
        if manifest["requiresToken"] and not token:
            raise PermissionError("device-token")
        # Recheck identity before sending any credentials to an address learned via mDNS.
        current = normalize_manifest(device_request(address, "manifest"))
        if current["id"] != device_id:
            raise ValueError("device-id")
        manifest = current
        state = state_for(manifest, device_request(address, "state", token=token))
        session_id = secrets.token_urlsafe(24)
        with self.lock:
            cutoff = time.monotonic() - 3600
            self.sessions = {key: value for key, value in self.sessions.items() if value[4] >= cutoff}
            if len(self.sessions) >= 100:
                self.sessions.pop(min(self.sessions, key=lambda key: self.sessions[key][4]))
            self.sessions[session_id] = (manifest, address, token, Lock(), time.monotonic())
        return {"session": session_id, "manifest": manifest, "state": state}

    def session(self, session_id, command=None):
        with self.lock:
            manifest, address, token, request_lock, last_seen = self.sessions[session_id]
            if time.monotonic() - last_seen >= 3600:
                del self.sessions[session_id]
                raise KeyError("session")
            self.sessions[session_id] = (manifest, address, token, request_lock, time.monotonic())
        if command is not None:
            cap = next((entry for entry in manifest["capabilities"] if entry["id"] == command.get("capability")), None)
            if cap is None or cap["type"] == "sensor" or not valid_value(cap, command.get("value")):
                raise ValueError("invalid-command")
        with request_lock:
            raw = device_request(address, "command" if command is not None else "state", command, token)
        return {"state": state_for(manifest, raw)}


REGISTRY = Registry()


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def guarded(self):
        return self.headers.get("Host") == HOST and self.headers.get("Origin") in (None, ORIGIN) and (
            self.headers.get("Sec-Fetch-Site") in (None, "none", "same-origin")
        )

    def reply(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.guarded():
            return self.reply(403, {"error": "wrong-origin"})
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            return super().do_GET()
        if path != "/api/discovery":
            return self.reply(404, {"error": "not-found"})
        try:
            return self.reply(200, {"devices": REGISTRY.scan()})
        except OSError:
            return self.reply(503, {"error": "discovery-unavailable"})

    def do_POST(self):
        if (not self.guarded() or self.headers.get("Origin") != ORIGIN
                or self.headers.get_content_type() != "application/json"):
            return self.reply(403, {"error": "wrong-origin"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("body")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("body")
            path = urlsplit(self.path).path
            if path == "/api/pair":
                device_id, token = value.get("id"), value.get("token", "")
                if (not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id)
                        or not isinstance(token, str) or not re.fullmatch(r"[!-~]{0,128}", token)):
                    raise ValueError("pair")
                return self.reply(200, REGISTRY.pair(device_id, token))
            if path in ("/api/state", "/api/command"):
                session_id = value.get("session")
                if not isinstance(session_id, str):
                    raise KeyError("session")
                command = {key: value.get(key) for key in ("capability", "value")} if path == "/api/command" else None
                return self.reply(200, REGISTRY.session(session_id, command))
            return self.reply(404, {"error": "not-found"})
        except PermissionError:
            return self.reply(401, {"error": "device-token"})
        except KeyError:
            return self.reply(404, {"error": "device-unavailable"})
        except (ValueError, TypeError):
            return self.reply(400, {"error": "invalid-request"})
        except (OSError, http.client.HTTPException):
            return self.reply(503, {"error": "device-unavailable"})


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", 8080), Handler) as server:
        print(f"Kontur: {ORIGIN}", flush=True)
        server.serve_forever()
