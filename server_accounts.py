"""SQLite account and shared home API for the local Kontur preview."""
from contextlib import contextmanager
from hashlib import scrypt, sha256
from hmac import compare_digest
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
import json
import math
import re
import secrets
import sqlite3
import time


SESSION_COOKIE = "kontur_session"
SESSION_AGE = 30 * 24 * 60 * 60
INVITATION_AGE = 24 * 60 * 60
ID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}", re.ASCII)
TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
EMAIL = re.compile(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+")
SCHEME = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash BLOB NOT NULL,
    salt BLOB NOT NULL,
    recovery_hash BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash BLOB PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS homes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    data TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS members (
    home_id TEXT NOT NULL REFERENCES homes(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'member')),
    PRIMARY KEY (home_id, user_id)
);
CREATE TABLE IF NOT EXISTS invitations (
    token_hash BLOB PRIMARY KEY,
    home_id TEXT NOT NULL REFERENCES homes(id) ON DELETE CASCADE,
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lan_bindings (
    device_id TEXT PRIMARY KEY,
    home_id TEXT NOT NULL REFERENCES homes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS homes_by_user ON members (user_id);
CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions (user_id);
"""


class AccountError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def require(test, status, code):
    if not test:
        raise AccountError(status, code)


def identifier(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def name(value, limit=60):
    require(short_text(value, limit) and value.strip(), 400, "invalid-name")
    return value.strip()


def short_text(value, limit):
    return (isinstance(value, str) and len(value) <= limit
            and not any(0xd800 <= ord(char) <= 0xdfff for char in value))


def address(value):
    require(isinstance(value, str) and len(value) <= 254 and EMAIL.fullmatch(value.strip()), 400, "invalid-email")
    return value.strip().casefold()


def password_bytes(value, new=False):
    require(isinstance(value, str) and (10 if new else 1) <= len(value) <= 128, 400, "invalid-password")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AccountError(400, "invalid-password") from exc


def digest(value):
    return sha256(value.encode("ascii")).digest()


def password_hash(value, salt):
    return scrypt(value, salt=salt, n=2**15, r=8, p=1, dklen=64, maxmem=64 * 1024**2)


def session_cookie(token, secure):
    expire = SESSION_AGE if token else 0
    return {
        "Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; Max-Age={expire}; HttpOnly; SameSite=Strict"
        + ("; Secure" if secure else "")
    }


def read_cookie(header):
    try:
        cookies = SimpleCookie(header or "")
        if SESSION_COOKIE in cookies:
            token = cookies[SESSION_COOKIE].value
            if identifier(token) and len(token) == 43:
                return digest(token)
    except CookieError:
        pass
    return None


def finite(value):
    return type(value) in (int, float) and abs(value) <= 1e100 and math.isfinite(value)


def valid_state_value(capability, value):
    kind = capability["type"]
    if kind == "switch":
        return type(value) is bool
    if not finite(value):
        return False
    return kind == "sensor" or capability["min"] <= value <= capability["max"]


def clean_capability(value):
    require(isinstance(value, dict), 400, "invalid-home-data")
    cap_id = value.get("id")
    kind = value.get("type")
    require(isinstance(cap_id, str) and CAPABILITY_ID.fullmatch(cap_id) and kind in ("switch", "slider", "sensor"),
            400, "invalid-home-data")
    result = {"id": cap_id, "type": kind, "label": name(value.get("label"))}
    for key, limit in (("unit", 12), ("description", 500)):
        text = value.get(key)
        if short_text(text, limit):
            result[key] = text
    low, high, step = (value.get(key) for key in ("min", "max", "step"))
    if kind == "slider":
        require(all(map(finite, (low, high, step))) and high > low and 0 < step <= high - low,
                400, "invalid-home-data")
        result.update(min=low, max=high, step=step)
    if kind == "sensor" and finite(low) and finite(high) and high > low:
        result.update(min=low, max=high)
    return result


def clean_schedule(value):
    require(isinstance(value, dict), 400, "invalid-schedule")
    result = {}
    mode = value.get("mode")
    if mode in ("manual", "auto", "interval"):
        result["mode"] = mode
    for key in ("on", "off"):
        daily = value.get(key)
        if isinstance(daily, str) and TIME.fullmatch(daily):
            result[key] = daily
    period = value.get("period")
    if type(period) is int and period in (15, 30, 60, 120, 240, 480, 1440):
        result["period"] = period
    threshold = value.get("threshold")
    if type(threshold) is int and 1 <= threshold <= 100:
        result["threshold"] = threshold
    next_run = value.get("nextRun")
    if type(next_run) is int and 0 < next_run < 9_007_199_254_740_991:
        result["nextRun"] = next_run
    return result


def clean_readings(capabilities, state):
    require(isinstance(state, dict), 400, "invalid-home-data")
    return {item["id"]: state[item["id"]] for item in capabilities
            if item["id"] in state and valid_state_value(item, state[item["id"]])}


def clean_home(value, previous=None):
    require(isinstance(value, dict), 400, "invalid-home-data")
    rooms, devices = (value.get(key) for key in ("rooms", "devices"))
    require(isinstance(rooms, list) and 1 <= len(rooms) <= 100
            and isinstance(devices, list) and len(devices) <= 300, 400, "invalid-home-data")
    clean_rooms = {}
    for room in rooms:
        require(isinstance(room, dict) and identifier(room.get("id")), 400, "invalid-home-data")
        room_id = room["id"]
        require(room_id not in clean_rooms, 400, "invalid-home-data")
        clean_rooms[room_id] = {"id": room_id, "name": name(room.get("name"))}
    previous_devices = {device["id"]: device for device in (previous or {}).get("devices", ())}
    clean_devices = {}
    for device in devices:
        require(isinstance(device, dict), 400, "invalid-home-data")
        device_id, room_id, capabilities = (device.get(key) for key in ("id", "roomId", "capabilities"))
        require(identifier(device_id) and device_id not in clean_devices and identifier(room_id)
                and room_id in clean_rooms and isinstance(capabilities, list) and len(capabilities) <= 20,
                400, "invalid-home-data")
        caps = [clean_capability(item) for item in capabilities]
        require(len({item["id"] for item in caps}) == len(caps), 400, "invalid-home-data")
        kind = device.get("kind")
        entry = {
            "id": device_id, "roomId": room_id, "name": name(device.get("name")),
            "nameIsDefault": device.get("nameIsDefault") is True,
            "kind": kind if isinstance(kind, str) and CAPABILITY_ID.fullmatch(kind) else "other",
            "capabilities": caps, "state": clean_readings(caps, device.get("state", {})),
            "online": device.get("online") is True
        }
        transport = device.get("transport")
        if transport in ("ble", "ble-test", "lan"):
            entry.update(transport=transport, online=False)
            old = previous_devices.get(device_id)
            if old and old.get("transport") == transport:
                entry["state"] = clean_readings(caps, old["state"])
                entry["online"] = old["online"]
                if "lastSeen" in old:
                    entry["lastSeen"] = old["lastSeen"]
        if isinstance(device.get("schedule"), dict):
            entry["schedule"] = clean_schedule(device["schedule"])
        clean_devices[device_id] = entry
    selected = value.get("roomId")
    return {
        "rooms": list(clean_rooms.values()), "devices": list(clean_devices.values()),
        "roomId": selected if identifier(selected) and selected in clean_rooms else rooms[0]["id"]
    }


class Accounts:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEME)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def route(self, method, path, body=None, cookie_header=None, secure=False):
        """Handle request after the HTTP adapter enforces same-origin and body size."""
        if method == "GET" and path == "/api/account/session":
            with self.connect() as db:
                return 200, self.account_snapshot(db, self.user(db, cookie_header)), {}
        if method != "POST":
            return None
        auth = {
            "/api/account/register": self.register, "/api/account/login": self.login,
            "/api/account/logout": self.logout, "/api/account/recover": self.recover,
            "/api/account/profile": self.rename
        }
        homes = {
            "/api/home/create": self.create_home, "/api/home/get": self.get_home,
            "/api/home/save": self.save_home, "/api/home/command": self.command,
            "/api/home/schedule": self.schedule, "/api/home/invite": self.invite,
            "/api/home/join": self.join, "/api/home/revoke": self.revoke
        }
        try:
            if path in auth:
                return auth[path](body or {}, cookie_header, secure)
            if path in homes:
                with self.connect(write=path != "/api/home/get") as db:
                    user = self.user(db, cookie_header)
                    require(user is not None, 401, "sign-in-required")
                    return 200, homes[path](db, user, body or {}), {}
        except AccountError as error:
            return error.status, {"error": error.code}, {}
        return None

    def user(self, db, cookie_header):
        token_hash = read_cookie(cookie_header)
        return db.execute(
            "SELECT id,email,name FROM users JOIN sessions ON users.id=sessions.user_id "
            "WHERE sessions.token_hash=? AND expires>?", (token_hash, int(time.time()))
        ).fetchone() if token_hash else None

    def account_snapshot(self, db, user):
        if user is None:
            return {"user": None, "homes": []}
        homes = db.execute(
            "SELECT homes.id,homes.name,members.role FROM homes JOIN members ON homes.id=members.home_id "
            "WHERE members.user_id=? ORDER BY homes.name,homes.id", (user["id"],)
        ).fetchall()
        return {"user": dict(user), "homes": [dict(home) for home in homes]}

    def start_session(self, db, user_id, cookie_header):
        db.execute("DELETE FROM sessions WHERE expires<=? OR token_hash=?", (int(time.time()), read_cookie(cookie_header)))
        token = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?)", (digest(token), user_id, int(time.time()) + SESSION_AGE)
        )
        return token

    def register(self, body, cookie_header, secure):
        email = address(body.get("email"))
        display_name = name(body.get("name"))
        plain = password_bytes(body.get("password"), new=True)
        user_id = secrets.token_hex(16)
        salt = secrets.token_bytes(16)
        recovery = secrets.token_urlsafe(24)
        hashed = password_hash(plain, salt)
        try:
            with self.connect(write=True) as db:
                db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                           (user_id, email, display_name, hashed, salt, digest(recovery)))
                token = self.start_session(db, user_id, cookie_header)
        except sqlite3.IntegrityError as exc:
            raise AccountError(409, "email-in-use") from exc
        return 201, {"user": {"id": user_id, "email": email, "name": display_name},
                     "homes": [], "recoveryCode": recovery}, session_cookie(token, secure)

    def login(self, body, cookie_header, secure):
        email = address(body.get("email"))
        plain = password_bytes(body.get("password"))
        with self.connect() as db:
            found = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        expected = found["password_hash"] if found else bytes(64)
        actual = password_hash(plain, found["salt"] if found else bytes(16))
        require(compare_digest(actual, expected) and found is not None, 401, "invalid-login")
        with self.connect(write=True) as db:
            user = db.execute(
                "SELECT id,email,name FROM users WHERE id=? AND password_hash=?",
                (found["id"], expected)
            ).fetchone()
            require(user is not None, 401, "invalid-login")
            token = self.start_session(db, user["id"], cookie_header)
            return 200, self.account_snapshot(db, user), session_cookie(token, secure)

    def logout(self, body, cookie_header, secure):
        token_hash = read_cookie(cookie_header)
        if token_hash:
            with self.connect(write=True) as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        return 200, {"user": None, "homes": []}, session_cookie("", secure)

    def recover(self, body, cookie_header, secure):
        email = address(body.get("email"))
        plain = password_bytes(body.get("password"), new=True)
        code = body.get("recoveryCode")
        require(identifier(code) and len(code) == 32, 401, "invalid-recovery")
        salt, recovery = secrets.token_bytes(16), secrets.token_urlsafe(24)
        hashed = password_hash(plain, salt)
        with self.connect(write=True) as db:
            found = db.execute(
                "SELECT id,email,name FROM users WHERE email=? AND recovery_hash=?", (email, digest(code))
            ).fetchone()
            require(found is not None, 401, "invalid-recovery")
            db.execute(
                "UPDATE users SET password_hash=?,salt=?,recovery_hash=? WHERE id=?",
                (hashed, salt, digest(recovery), found["id"])
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (found["id"],))
            token = self.start_session(db, found["id"], cookie_header)
            return 200, {**self.account_snapshot(db, found), "recoveryCode": recovery}, session_cookie(token, secure)

    def rename(self, body, cookie_header, secure):
        display_name = name(body.get("name"))
        with self.connect(write=True) as db:
            user = self.user(db, cookie_header)
            require(user is not None, 401, "sign-in-required")
            db.execute("UPDATE users SET name=? WHERE id=?", (display_name, user["id"]))
            return 200, self.account_snapshot(db, {**dict(user), "name": display_name}), {}

    def authorize(self, cookie_header, home_id, owner=False):
        with self.connect() as db:
            user = self.user(db, cookie_header)
            require(user is not None, 401, "sign-in-required")
            membership = self.membership(db, user, home_id, owner)
            return {"userId": user["id"], "homeId": membership["id"], "role": membership["role"]}

    def membership(self, db, user, home_id, owner=False):
        require(identifier(home_id), 400, "invalid-home")
        home = db.execute(
            "SELECT homes.*,members.role FROM homes JOIN members ON home_id=homes.id "
            "WHERE homes.id=? AND user_id=?", (home_id, user["id"])
        ).fetchone()
        require(home is not None, 403, "home-access-denied")
        require(not owner or home["role"] == "owner", 403, "owner-required")
        return home

    def home_snapshot(self, db, home, user):
        people = db.execute(
            "SELECT users.id,users.name,role FROM members JOIN users ON users.id=user_id "
            "WHERE home_id=? ORDER BY role DESC,users.name,users.id", (home["id"],)
        ).fetchall()
        state = json.loads(home["data"])
        state["profile"] = {
            "name": user["name"], "customName": True,
            "role": "Админ" if home["role"] == "owner" else "Пользователь"
        }
        return {"id": home["id"], "name": home["name"], "role": home["role"],
                "state": state, "revision": home["revision"], "members": [dict(item) for item in people]}

    def create_home(self, db, user, body):
        home_name = name(body.get("name"))
        state = clean_home(body["state"]) if "state" in body else {
            "rooms": [{"id": "living", "name": "Гостиная"}], "devices": [], "roomId": "living"
        }
        home_id = secrets.token_hex(16)
        db.execute("INSERT INTO homes (id,name,data) VALUES (?,?,?)",
                   (home_id, home_name, json.dumps(state, ensure_ascii=False, allow_nan=False)))
        db.execute("INSERT INTO members VALUES (?,?,?)", (home_id, user["id"], "owner"))
        return self.home_snapshot(db, self.membership(db, user, home_id), user)

    def get_home(self, db, user, body):
        return self.home_snapshot(db, self.membership(db, user, body.get("homeId")), user)

    def store_home(self, db, home_id, state):
        db.execute("UPDATE homes SET data=?,revision=revision+1 WHERE id=?",
                   (json.dumps(state, ensure_ascii=False, allow_nan=False), home_id))

    def save_home(self, db, user, body):
        home = self.membership(db, user, body.get("homeId"), owner=True)
        revision = body.get("revision")
        require(type(revision) is int and revision == home["revision"], 409, "revision-conflict")
        previous = json.loads(home["data"])
        state = clean_home(body.get("state"), previous)
        old_devices = {item["id"]: item for item in previous["devices"]}
        devices = {item["id"]: item for item in state["devices"]}
        bound = db.execute("SELECT device_id FROM lan_bindings WHERE home_id=?", (home["id"],)).fetchall()
        for item in bound:
            device_id = item["device_id"]
            if device_id not in devices:
                db.execute("DELETE FROM lan_bindings WHERE home_id=? AND device_id=?", (home["id"], device_id))
                continue
            old = old_devices[device_id]
            updated = devices[device_id]
            for key in ("kind", "transport", "capabilities", "state", "online", "lastSeen"):
                if key in old:
                    updated[key] = old[key]
        self.store_home(db, home["id"], state)
        return self.home_snapshot(db, self.membership(db, user, home["id"]), user)

    def device_for(self, data, device_id):
        require(identifier(device_id), 400, "invalid-device")
        device = next((item for item in data["devices"] if item["id"] == device_id), None)
        require(device is not None, 404, "device-not-found")
        return device

    def command(self, db, user, body):
        home = self.membership(db, user, body.get("homeId"))
        state = json.loads(home["data"])
        device = self.device_for(state, body.get("id"))
        require(not device.get("transport"), 409, "hardware-command-required")
        require(device["online"], 409, "device-offline")
        command = body.get("capability")
        capability = next((item for item in device["capabilities"] if item["id"] == command), None)
        value = body.get("value")
        require(capability is not None and capability["type"] != "sensor" and valid_state_value(capability, value),
                400, "invalid-command")
        device["state"][command] = value
        self.store_home(db, home["id"], state)
        return self.home_snapshot(db, self.membership(db, user, home["id"]), user)

    def schedule(self, db, user, body):
        home = self.membership(db, user, body.get("homeId"))
        state = json.loads(home["data"])
        device = self.device_for(state, body.get("id"))
        require(any(item["id"] == "power" and item["type"] == "switch" for item in device["capabilities"]),
                400, "invalid-schedule")
        schedule = clean_schedule(body.get("schedule"))
        require(schedule.get("mode") != "auto" or device["kind"] in ("humidifier", "purifier"),
                400, "invalid-schedule")
        device["schedule"] = schedule
        self.store_home(db, home["id"], state)
        return self.home_snapshot(db, self.membership(db, user, home["id"]), user)

    def update_device_state(self, home_id, device_id, state, online):
        """Update trusted hardware telemetry. Call after adapter authentication."""
        with self.connect(write=True) as db:
            linked = db.execute(
                "SELECT 1 FROM lan_bindings WHERE home_id=? AND device_id=?", (home_id, device_id)
            ).fetchone()
            require(linked, 404, "device-not-found")
            home = db.execute("SELECT data FROM homes WHERE id=?", (home_id,)).fetchone()
            require(home is not None, 404, "invalid-home")
            data = json.loads(home["data"])
            device = self.device_for(data, device_id)
            require(device.get("transport"), 400, "invalid-device")
            was_online = device["online"]
            device["online"] = online is True
            if online:
                readings = clean_readings(device["capabilities"], state)
                if not readings or readings == device["state"] and was_online:
                    # Keep snapshots stable for owner revisions, but refresh the persisted
                    # sample time without treating an unchanged reading as a user edit.
                    if readings:
                        device["lastSeen"] = int(time.time() * 1000)
                        db.execute("UPDATE homes SET data=? WHERE id=?",
                                   (json.dumps(data, ensure_ascii=False, allow_nan=False), home_id))
                    return
                device["state"] = readings
                device["lastSeen"] = int(time.time() * 1000)
            elif not was_online:
                return
            self.store_home(db, home_id, data)

    def mark_lan_offline(self):
        """Invalidate connectivity carried over from a previous bridge process."""
        with self.connect(write=True) as db:
            homes = db.execute("SELECT id,data FROM homes").fetchall()
            for home in homes:
                data = json.loads(home["data"])
                online = [device for device in data["devices"]
                          if device.get("transport") == "lan" and device["online"]]
                if online:
                    for device in online:
                        device["online"] = False
                    self.store_home(db, home["id"], data)

    def has_device(self, home_id, device_id):
        return self.lan_owner(device_id) == home_id

    def lan_owner(self, device_id):
        with self.connect() as db:
            binding = db.execute("SELECT home_id FROM lan_bindings WHERE device_id=?", (device_id,)).fetchone()
            return binding["home_id"] if binding else None

    def bind_device(self, home_id, manifest, room_id, state):
        """Record a manifest and readings already confirmed by the LAN adapter."""
        physical_id = manifest.get("id")
        require(identifier(physical_id) and len(physical_id) <= 64, 400, "invalid-device")
        device_id = "lan-" + physical_id
        with self.connect(write=True) as db:
            claim = db.execute("SELECT home_id FROM lan_bindings WHERE device_id=?", (device_id,)).fetchone()
            require(claim is None or claim["home_id"] == home_id, 409, "device-owned")
            home = db.execute("SELECT data FROM homes WHERE id=?", (home_id,)).fetchone()
            require(home is not None, 404, "invalid-home")
            data = json.loads(home["data"])
            require(any(room["id"] == room_id for room in data["rooms"]), 400, "invalid-room")
            new_data = clean_home({
                "rooms": data["rooms"], "devices": [{
                    "id": device_id, "roomId": room_id, "transport": "lan",
                    "name": manifest.get("name"), "kind": manifest.get("kind"),
                    "capabilities": manifest.get("capabilities"), "state": state
                }]
            })
            device = new_data["devices"][0]
            device.update(online=True, lastSeen=int(time.time() * 1000))
            old = next((item for item in data["devices"] if item["id"] == device_id), None)
            if old is None:
                require(len(data["devices"]) < 300, 400, "invalid-home-data")
                data["devices"].append(device)
            else:
                if "schedule" in old:
                    device["schedule"] = old["schedule"]
                data["devices"][data["devices"].index(old)] = device
            db.execute("INSERT OR IGNORE INTO lan_bindings VALUES (?,?)", (device_id, home_id))
            self.store_home(db, home_id, data)
            return device

    def has_membership(self, user_id, home_id):
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM members WHERE user_id=? AND home_id=?", (user_id, home_id)
            ).fetchone() is not None

    def iter_homes(self):
        with self.connect() as db:
            homes = db.execute("SELECT id,name,data,revision FROM homes ORDER BY id").fetchall()
        return [{"id": home["id"], "name": home["name"], "state": json.loads(home["data"]),
                 "revision": home["revision"]} for home in homes]

    def apply_automation(self, home_id, device_id, expected_revision, power=None, next_run=None):
        """Advance a rule only while its input snapshot is current."""
        with self.connect(write=True) as db:
            home = db.execute("SELECT data,revision FROM homes WHERE id=?", (home_id,)).fetchone()
            if home is None or home["revision"] != expected_revision:
                return False
            state = json.loads(home["data"])
            device = self.device_for(state, device_id)
            schedule = device.get("schedule")
            if not schedule or not device["online"]:
                return False
            if power is not None:
                if device.get("transport") or type(power) is not bool or not any(
                    item["id"] == "power" and item["type"] == "switch" for item in device["capabilities"]
                ):
                    return False
                device["state"]["power"] = power
            if next_run is not None:
                if schedule.get("mode") != "interval" or type(next_run) is not int or next_run < 1:
                    return False
                schedule["nextRun"] = next_run
            self.store_home(db, home_id, state)
            return True

    def invite(self, db, user, body):
        home = self.membership(db, user, body.get("homeId"), owner=True)
        db.execute("DELETE FROM invitations WHERE expires<=?", (int(time.time()),))
        code = secrets.token_urlsafe(24)
        db.execute("INSERT INTO invitations VALUES (?,?,?)",
                   (digest(code), home["id"], int(time.time()) + INVITATION_AGE))
        return {"code": code}

    def join(self, db, user, body):
        code = body.get("code")
        require(identifier(code) and len(code) == 32, 404, "invalid-invitation")
        found = db.execute("SELECT home_id FROM invitations WHERE token_hash=? AND expires>?",
                           (digest(code), int(time.time()))).fetchone()
        require(found is not None, 404, "invalid-invitation")
        home_id = found["home_id"]
        existing = db.execute("SELECT 1 FROM members WHERE home_id=? AND user_id=?", (home_id, user["id"])).fetchone()
        require(not existing, 409, "already-member")
        db.execute("INSERT INTO members VALUES (?,?,?)", (home_id, user["id"], "member"))
        db.execute("DELETE FROM invitations WHERE token_hash=?", (digest(code),))
        return self.home_snapshot(db, self.membership(db, user, home_id), user)

    def revoke(self, db, user, body):
        home = self.membership(db, user, body.get("homeId"), owner=True)
        user_id = body.get("userId")
        require(identifier(user_id) and user_id != user["id"], 400, "invalid-member")
        db.execute("DELETE FROM members WHERE home_id=? AND user_id=? AND role='member'", (home["id"], user_id))
        return self.home_snapshot(db, home, user)
