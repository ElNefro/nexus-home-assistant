"""Loopback-only bridge between the Kontur dashboard and local Wi-Fi devices."""
import http.client
import ipaddress
import json
import math
import logging
import os
import re
import secrets
import selectors
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlsplit

from ifaddr import get_adapters
from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

from server_accounts import Accounts, AccountError
from server_automation import decision


SERVICE = "_kontur._tcp.local."
DISCOVERY_PORT = 42424
DISCOVERY_REQUEST = b"KONTUR-DISCOVER-1 "
DISCOVERY_RESPONSE = b"KONTUR-HERE-1 "
PUBLIC = Path(__file__).resolve().parent / "public"
HOST = "127.0.0.1:8080"
ORIGIN = f"http://{HOST}"
LOCAL_HOSTS = {HOST, "localhost:8080"}
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
    pair_signal = value.get("pairSignal", False)
    requires_token = value.get("requiresToken") is not False
    if type(pair_signal) is not bool or (pair_signal and not requires_token):
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
        if "momentary" in entry and (type(entry["momentary"]) is not bool or cap_type != "switch"):
            raise ValueError("capability")
        if cap_type == "slider":
            lo, hi, step = (entry.get(key) for key in ("min", "max", "step"))
            if not all(map(finite, (lo, hi, step))) or hi <= lo or not 0 < step <= hi - lo:
                raise ValueError("capability")
    manifest = {"schemaVersion": 1, "id": device_id, "name": name.strip(), "kind": kind,
                "capabilities": capabilities, "requiresToken": requires_token}
    if pair_signal:
        manifest["pairSignal"] = True
    return manifest


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
    method = "POST" if route == "pair" or command is not None else "GET"
    if command is not None:
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


def dns_sd_records():
    listener = Devices()
    with Zeroconf(ip_version=IPVersion.V4Only) as zeroconf:
        browser = ServiceBrowser(zeroconf, SERVICE, listener)
        try:
            time.sleep(1.6)
        finally:
            browser.cancel()
        return list(listener.records.values())[:20]


def broadcast_addresses():
    nonce = secrets.token_hex(12).encode("ascii")
    targets = set()
    try:
        for adapter in get_adapters():
            for address in adapter.ips:
                if address.is_IPv4 and 0 < address.network_prefix < 31:
                    interface = ipaddress.IPv4Interface((address.ip, address.network_prefix))
                    if any(interface.ip in network for network in LOCAL_NETWORKS):
                        targets.add(interface)
    except (OSError, ValueError):
        pass
    addresses = set()
    channels = []
    with selectors.DefaultSelector() as selector:
        try:
            for interface in sorted(targets)[:16]:
                channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                channels.append(channel)
                try:
                    channel.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    channel.bind((str(interface.ip), 0))
                    channel.setblocking(False)
                    channel.sendto(DISCOVERY_REQUEST + nonce, (str(interface.network.broadcast_address), DISCOVERY_PORT))
                    selector.register(channel, selectors.EVENT_READ, interface)
                except OSError:
                    continue
            if not selector.get_map():
                return []
            deadline = time.monotonic() + 1.25
            while time.monotonic() < deadline and len(addresses) < 20:
                for key, _ in selector.select(max(0, deadline - time.monotonic())):
                    try:
                        payload, (host, port) = key.fileobj.recvfrom(64)
                    except OSError:
                        continue
                    if port != DISCOVERY_PORT or payload != DISCOVERY_RESPONSE + nonce:
                        continue
                    ip = ipaddress.IPv4Address(host)
                    interface = key.data
                    if (ip in interface.network and ip not in (interface.ip, interface.network.broadcast_address,
                            interface.network.network_address) and any(ip in network for network in LOCAL_NETWORKS)):
                        addresses.add((host, 80))
        finally:
            for channel in channels:
                channel.close()
    return sorted(addresses)[:20]


def resolve_broadcast(address):
    try:
        return normalize_manifest(device_request(address, "manifest")), address
    except (OSError, ValueError, http.client.HTTPException):
        return None


def discover():
    with ThreadPoolExecutor(max_workers=10) as pool:
        broadcast = pool.submit(broadcast_addresses)
        try:
            records = dns_sd_records()
        except OSError:
            records = []
        try:
            addresses = broadcast.result()
        except OSError:
            addresses = []
        found = [*pool.map(resolve_record, records), *pool.map(resolve_broadcast, addresses)]
        return [entry for entry in found if entry]


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
        # Recheck identity before sending credentials to a discovered address.
        current = normalize_manifest(device_request(address, "manifest"))
        if current["id"] != device_id:
            raise ValueError("device-id")
        manifest = current
        route = "pair" if manifest.get("pairSignal") else "state"
        state = state_for(manifest, device_request(address, route, token=token))
        session_id = secrets.token_urlsafe(24)
        with self.lock:
            cutoff = time.monotonic() - 3600
            self.sessions = {key: value for key, value in self.sessions.items() if value[4] >= cutoff}
            if len(self.sessions) >= 100:
                self.sessions.pop(min(self.sessions, key=lambda key: self.sessions[key][4]))
            self.sessions[session_id] = (manifest, address, token, Lock(), time.monotonic())
        return {"session": session_id, "manifest": manifest, "state": state}

    def device_id_for_session(self, session_id):
        with self.lock:
            return "lan-" + self.sessions[session_id][0]["id"]

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


class SharedDevices:
    def __init__(self, accounts, push):
        self.accounts = accounts
        self.push = push
        self.links = {}
        self.lock = Lock()

    def add(self, home_id, room_id, pair):
        device = self.accounts.bind_device(home_id, pair["manifest"], room_id, pair["state"])
        with self.lock:
            self.links[(home_id, device["id"])] = pair["session"]

    def linked(self, home_id, device_id):
        with self.lock:
            session = self.links.get((home_id, device_id))
        return session if session and self.accounts.has_device(home_id, device_id) else None

    def read(self, home_id, device_id, session, command=None):
        state = REGISTRY.session(session, command)["state"]
        self.accounts.update_device_state(home_id, device_id, state, True)
        if command is not None and state.get(command["capability"]) != command["value"]:
            raise AccountError(502, "device-unconfirmed")
        return state

    def tick(self, previous_minute):
        now = int(time.time() * 1000)
        for home in self.accounts.iter_homes():
            devices = home["state"]["devices"]
            revision = home["revision"]
            for device in devices:
                device_id = device["id"]
                power, next_run = decision(device, devices, now, previous_minute)
                if power is None and next_run is None:
                    continue
                if device.get("transport"):
                    session = self.linked(home["id"], device_id)
                    if not session:
                        continue
                    if power is not None:
                        try:
                            # Re-read next tick so a failed command does not consume the interval.
                            self.read(home["id"], device_id, session, {"capability":"power","value":power})
                        except (OSError, KeyError, ValueError, AccountError, http.client.HTTPException):
                            continue
                        break
                if self.accounts.apply_automation(home["id"], device_id, revision, power, next_run):
                    revision += 1
        return now // 60_000

    def poll(self):
        with self.lock:
            links = list(self.links.items())
        for (home_id, device_id), session in links:
            if not self.accounts.has_device(home_id,device_id):
                with self.lock:
                    if self.links.get((home_id, device_id)) == session:
                        self.links.pop((home_id, device_id), None)
                continue
            try:
                self.read(home_id, device_id, session)
            except (OSError, KeyError, ValueError, AccountError, http.client.HTTPException):
                try:
                    self.accounts.update_device_state(home_id, device_id, {}, False)
                except AccountError:
                    pass
        if self.push:
            for home in self.accounts.iter_homes():
                for device in home["state"]["devices"]:
                    self.push.observe(home["id"],device)

    def run(self, stopped):
        previous_minute = None
        next_poll = 0
        while not stopped.wait(1):
            try:
                previous_minute = self.tick(previous_minute)
                if time.monotonic() >= next_poll:
                    self.poll()
                    next_poll = time.monotonic() + 4
            except Exception:
                logging.exception("Local home worker")



class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def guarded(self):
        host = self.headers.get("Host")
        return host in LOCAL_HOSTS and self.headers.get("Origin") in (None, f"http://{host}") and (
            self.headers.get("Sec-Fetch-Site") in (None, "none", "same-origin")
        )

    def dashboard_navigation(self, path):
        # A click from the published site can open the shell as a top-level page.
        # API calls continue to require the local origin.
        return (
            path in ("/", "/index.html")
            and self.headers.get("Host") in LOCAL_HOSTS
            and self.headers.get("Origin") is None
            and self.headers.get("Sec-Fetch-Mode") == "navigate"
            and self.headers.get("Sec-Fetch-Dest") == "document"
            and self.headers.get("Sec-Fetch-User") == "?1"
        )

    def reply(self, code, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name,value)
        self.end_headers()
        self.wfile.write(body)

    def shared_route(self, method, path, value=None):
        cookie = self.headers.get("Cookie")
        if self.server.push:
            result = self.server.push.route(method,path,value,cookie)
            if result is not None:
                return result
        result = self.server.accounts.route(method,path,value,cookie)
        if self.server.push and path == "/api/home/revoke" and result and result[0] == 200:
            self.server.push.after_revoke(value["userId"],value["homeId"])
        return result

    def get_home(self, value):
        return self.server.accounts.route("POST", "/api/home/get", {"homeId":value["homeId"]}, self.headers.get("Cookie"))

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.guarded() and not self.dashboard_navigation(path):
            return self.reply(403, {"error": "wrong-origin"})
        if not path.startswith("/api/"):
            return super().do_GET()
        handled = self.shared_route("GET",path)
        if handled is not None:
            return self.reply(*handled)
        if path != "/api/discovery":
            return self.reply(404, {"error": "not-found"})
        try:
            return self.reply(200, {"devices": REGISTRY.scan()})
        except OSError:
            return self.reply(503, {"error": "discovery-unavailable"})

    def do_POST(self):
        if (not self.guarded() or self.headers.get("Origin") != f'http://{self.headers.get("Host")}'
                or self.headers.get_content_type() != "application/json"):
            return self.reply(403, {"error": "wrong-origin"})
        try:
            path = urlsplit(self.path).path
            limit = 2_000_000 if path in ("/api/home/create", "/api/home/save") else 4096
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= limit:
                raise ValueError("body")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("body")
            if path == "/api/home/command" and isinstance(value.get("id"), str) and value["id"].startswith("lan-"):
                home_id, device_id = value.get("homeId"),value["id"]
                self.server.accounts.authorize(self.headers.get("Cookie"),home_id)
                session = self.server.shared_devices.linked(home_id,device_id)
                if not session:
                    raise AccountError(409,"device-offline")
                self.server.shared_devices.read(home_id,device_id,session,{key:value.get(key) for key in ("capability","value")})
                return self.reply(*self.get_home(value))
            handled = self.shared_route("POST",path,value)
            if handled is not None:
                return self.reply(*handled)
            if path == "/api/pair":
                device_id, token = value.get("id"), value.get("token", "")
                if (not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id)
                        or not isinstance(token, str) or not re.fullmatch(r"[!-~]{0,128}", token)):
                    raise ValueError("pair")
                if "homeId" in value:
                    home_id = value["homeId"]
                    self.server.accounts.authorize(self.headers.get("Cookie"),home_id,owner=True)
                    self.server.shared_devices.add(home_id,value.get("roomId"),REGISTRY.pair(device_id, token))
                    return self.reply(*self.get_home(value))
                if self.server.accounts.lan_owner("lan-" + device_id):
                    raise AccountError(409, "device-owned")
                return self.reply(200, REGISTRY.pair(device_id, token))
            if path in ("/api/state", "/api/command"):
                session_id = value.get("session")
                if not isinstance(session_id, str):
                    raise KeyError("session")
                if self.server.accounts.lan_owner(REGISTRY.device_id_for_session(session_id)):
                    raise AccountError(409, "device-owned")
                command = {key: value.get(key) for key in ("capability", "value")} if path == "/api/command" else None
                return self.reply(200, REGISTRY.session(session_id, command))
            return self.reply(404, {"error": "not-found"})
        except AccountError as error:
            return self.reply(error.status, {"error":error.code})
        except PermissionError as error:
            code = str(error)
            return self.reply(401 if code == "device-token" else 503, {"error":"device-token" if code == "device-token" else "device-unavailable"})
        except KeyError:
            return self.reply(404, {"error": "device-unavailable"})
        except (ValueError, TypeError):
            return self.reply(400, {"error": "invalid-request"})
        except (OSError, http.client.HTTPException):
            return self.reply(503, {"error": "device-unavailable"})


def run():
    root = Path(__file__).resolve().parent
    database = Path(os.getenv("KONTUR_DB", str(root / ".local-work" / "kontur-preview.sqlite3")))
    database.parent.mkdir(parents=True, exist_ok=True)
    stopped = Event()
    with ThreadingHTTPServer(("127.0.0.1", 8080), Handler) as server:
        server.accounts = Accounts(database)
        server.accounts.mark_lan_offline()
        try:
            from shared_push_routes import PushRoutes
            server.push = PushRoutes(database.with_name(database.stem + "-push.sqlite3"),server.accounts)
        except ImportError:
            server.push = None
        server.shared_devices = SharedDevices(server.accounts,server.push)
        worker = Thread(target=server.shared_devices.run,args=(stopped,),daemon=True)
        worker.start()
        print("Kontur: http://localhost:8080 (http://127.0.0.1:8080 for existing local data)", flush=True)
        try:
            server.serve_forever()
        finally:
            stopped.set()


if __name__ == "__main__":
    run()
