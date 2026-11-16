"""Account and shared home API for local and cloud deployments."""
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

from server_automation import has_schedule_power


SESSION_COOKIE = "kontur_session"
SESSION_AGE = 30 * 24 * 60 * 60
INVITATION_AGE = 24 * 60 * 60
BINDING_TABLES = {"lan": "lan_bindings", "cloud": "cloud_bindings"}
ID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}", re.ASCII)
DEVICE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)
CLOUD_ENROLLMENT_NONCE = re.compile(r"[a-f0-9]{32}", re.ASCII)
HOME_INDEX = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", re.ASCII)
TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
EMAIL = re.compile(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+")
SCHEME = """
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
CREATE TABLE IF NOT EXISTS cloud_bindings (
    device_id TEXT PRIMARY KEY,
    home_id TEXT NOT NULL REFERENCES homes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS cloud_enrollment_uses (
    device_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    home_id TEXT NOT NULL REFERENCES homes(id) ON DELETE CASCADE,
    binding_id TEXT REFERENCES cloud_bindings(device_id) ON DELETE SET NULL,
    PRIMARY KEY (device_id, nonce)
);
CREATE TABLE IF NOT EXISTS home_indices (
    home_index TEXT PRIMARY KEY,
    home_id TEXT NOT NULL UNIQUE REFERENCES homes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS member_prefixes (
    home_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    prefix TEXT NOT NULL,
    PRIMARY KEY (home_id, user_id),
    FOREIGN KEY (home_id, user_id) REFERENCES members(home_id, user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS homes_by_user ON members (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS one_owner_per_home ON members (home_id) WHERE role='owner';
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
            and not any(char == "\x00" or 0xd800 <= ord(char) <= 0xdfff for char in value))


def home_index(value):
    if not isinstance(value, str) or not value.isascii():
        raise AccountError(400, "invalid-home-index")
    cleaned = value.strip().upper()
    require(3 <= len(cleaned) <= 32 and HOME_INDEX.fullmatch(cleaned), 400, "invalid-home-index")
    return cleaned


def address(value):
    require(short_text(value, 254) and EMAIL.fullmatch(value.strip()), 400, "invalid-email")
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
    if kind == "switch" and value.get("momentary") is True:
        result["momentary"] = True
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
    readings = {}
    for item in capabilities:
        key = item["id"]
        if item.get("momentary") is True:
            readings[key] = False
        elif key in state and valid_state_value(item, state[key]):
            readings[key] = state[key]
    return readings


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
        if transport in ("ble", "ble-test", "lan", "cloud"):
            entry.update(transport=transport, online=False)
            old = previous_devices.get(device_id)
            if old and old.get("transport") == transport:
                entry["state"] = clean_readings(caps, old["state"])
                entry["online"] = old["online"]
                if "lastSeen" in old:
                    entry["lastSeen"] = old["lastSeen"]
        if has_schedule_power(caps) and isinstance(device.get("schedule"), dict):
            entry["schedule"] = clean_schedule(device["schedule"])
        clean_devices[device_id] = entry
    selected = value.get("roomId")
    return {
        "rooms": list(clean_rooms.values()), "devices": list(clean_devices.values()),
        "roomId": selected if identifier(selected) and selected in clean_rooms else rooms[0]["id"]
    }


class Accounts:
    def __init__(self, path=None, *, database_url=None):
        self.postgres = None
        if database_url:
            from server_accounts_postgres import PostgresStore

            self.postgres = PostgresStore(database_url, SCHEME)
            self.integrity_error = self.postgres.integrity_error
            return
        if path is None:
            raise ValueError("An accounts database path or URL is required")
        self.integrity_error = sqlite3.IntegrityError
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEME)

    @contextmanager
    def connect(self, write=False):
        if self.postgres:
            with self.postgres.connect(write=write) as db:
                yield db
            return
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
            "/api/home/join": self.join, "/api/home/revoke": self.revoke,
            "/api/home/member-prefix": self.member_prefix
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
            "SELECT homes.id,homes.name,members.role,home_indices.home_index "
            "FROM homes JOIN members ON homes.id=members.home_id "
            "LEFT JOIN home_indices ON homes.id=home_indices.home_id "
            "WHERE members.user_id=? ORDER BY homes.name,homes.id", (user["id"],)
        ).fetchall()
        return {"user": dict(user), "homes": [
            {"id": home["id"], "name": home["name"], "role": home["role"], "index": home["home_index"]}
            for home in homes
        ]}

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
        except self.integrity_error as exc:
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
            "SELECT users.id,users.name,members.role,member_prefixes.prefix "
            "FROM members JOIN users ON users.id=members.user_id "
            "LEFT JOIN member_prefixes ON members.home_id=member_prefixes.home_id "
            "AND members.user_id=member_prefixes.user_id "
            "WHERE members.home_id=? ORDER BY members.role DESC,users.name,users.id", (home["id"],)
        ).fetchall()
        members = [{**dict(item), "prefix": item["prefix"] or ("Админ" if item["role"] == "owner" else "Семья")}
                   for item in people]
        own_prefix = next((item["prefix"] for item in members if item["id"] == user["id"]), None)
        index_row = db.execute("SELECT home_index FROM home_indices WHERE home_id=?", (home["id"],)).fetchone()
        state = json.loads(home["data"])
        state["profile"] = {
            "name": user["name"], "customName": True, "prefix": own_prefix,
            "role": "Админ" if home["role"] == "owner" else "Пользователь"
        }
        return {"id": home["id"], "name": home["name"], "role": home["role"], "prefix": own_prefix,
                "index": index_row["home_index"] if index_row else None,
                "state": state, "revision": home["revision"], "members": members}

    def create_home(self, db, user, body):
        home_name = name(body.get("name"))
        index = home_index(body["index"]) if "index" in body else None
        if index:
            existing = db.execute("SELECT 1 FROM home_indices WHERE home_index=?", (index,)).fetchone()
            require(existing is None, 409, "home-index-in-use")
        state = clean_home(body["state"]) if "state" in body else {
            "rooms": [{"id": "living", "name": "Гостиная"}], "devices": [], "roomId": "living"
        }
        home_id = secrets.token_hex(16)
        db.execute("INSERT INTO homes (id,name,data) VALUES (?,?,?)",
                   (home_id, home_name, json.dumps(state, ensure_ascii=False, allow_nan=False)))
        db.execute("INSERT INTO members VALUES (?,?,?)", (home_id, user["id"], "owner"))
        if index:
            db.execute("INSERT INTO home_indices VALUES (?,?)", (index, home_id))
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
        for table in BINDING_TABLES.values():
            bound = db.execute(f"SELECT device_id FROM {table} WHERE home_id=?", (home["id"],)).fetchall()
            for item in bound:
                device_id = item["device_id"]
                if device_id not in devices:
                    db.execute(f"DELETE FROM {table} WHERE home_id=? AND device_id=?", (home["id"], device_id))
                    continue
                old = old_devices[device_id]
                updated = devices[device_id]
                for key in ("kind", "transport", "capabilities", "state", "online", "lastSeen"):
                    if key in old:
                        updated[key] = old[key]
                if not has_schedule_power(updated["capabilities"]):
                    updated.pop("schedule", None)
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
        device["state"][command] = False if capability.get("momentary") is True else value
        self.store_home(db, home["id"], state)
        return self.home_snapshot(db, self.membership(db, user, home["id"]), user)

    def schedule(self, db, user, body):
        home = self.membership(db, user, body.get("homeId"))
        state = json.loads(home["data"])
        device = self.device_for(state, body.get("id"))
        require(has_schedule_power(device["capabilities"]), 400, "invalid-schedule")
        schedule = clean_schedule(body.get("schedule"))
        require(schedule.get("mode") != "auto" or device["kind"] in ("humidifier", "purifier"),
                400, "invalid-schedule")
        device["schedule"] = schedule
        self.store_home(db, home["id"], state)
        return self.home_snapshot(db, self.membership(db, user, home["id"]), user)

    def update_device_state(self, home_id, device_id, state, online):
        """Update trusted hardware telemetry. Call after adapter authentication."""
        table = BINDING_TABLES.get(device_id.split("-", 1)[0])
        require(table is not None, 404, "device-not-found")
        with self.connect(write=True) as db:
            linked = db.execute(
                f"SELECT 1 FROM {table} WHERE home_id=? AND device_id=?", (home_id, device_id)
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
        table = BINDING_TABLES.get(device_id.split("-", 1)[0])
        return table is not None and home_id is not None and self.binding_owner(table, device_id) == home_id

    def lan_owner(self, device_id):
        return self.binding_owner("lan_bindings", device_id)

    def cloud_owner(self, device_id):
        return self.binding_owner("cloud_bindings", device_id)

    def binding_owner(self, table, device_id):
        with self.connect() as db:
            binding = self.lookup_binding(db, table, device_id)
            return binding["home_id"] if binding else None

    def lookup_binding(self, db, table, device_id):
        return db.execute(f"SELECT home_id FROM {table} WHERE device_id=?", (device_id,)).fetchone()

    def cloud_enrollment_applied(self, home_id, device_id, nonce):
        with self.connect() as db:
            return self._cloud_enrollment_applied(db, home_id, device_id, nonce)

    def _cloud_enrollment_applied(self, db, home_id, device_id, nonce):
        record = db.execute(
            "SELECT home_id,binding_id FROM cloud_enrollment_uses WHERE device_id=? AND nonce=?",
            (device_id, nonce)
        ).fetchone()
        if record is None:
            return False
        # Unbinding nulls this reference permanently, even if another enrollment binds the device later.
        require(record["home_id"] == home_id and record["binding_id"] == device_id, 410, "enrollment-expired")
        return True

    def bind_device(self, home_id, manifest, room_id, state):
        """Record a manifest and readings already confirmed by the LAN adapter."""
        return self.bind_physical_device("lan", home_id, manifest, room_id, state, online=True)

    def bind_cloud_device(self, home_id, manifest, room_id, *, online=False, enrollment_nonce=None):
        """Record an authenticated manifest from the cloud transport."""
        if enrollment_nonce is not None:
            require(isinstance(enrollment_nonce, str) and CLOUD_ENROLLMENT_NONCE.fullmatch(enrollment_nonce),
                    400, "invalid-enrollment")
        return self.bind_physical_device(
            "cloud", home_id, manifest, room_id, {}, online=online, enrollment_nonce=enrollment_nonce
        )

    def bind_physical_device(self, transport, home_id, manifest, room_id, state, online, enrollment_nonce=None):
        physical_id = manifest.get("id")
        require(isinstance(physical_id, str) and DEVICE_ID.fullmatch(physical_id), 400, "invalid-device")
        device_id = transport + "-" + physical_id
        with self.connect(write=True) as db:
            if enrollment_nonce is not None and self._cloud_enrollment_applied(db, home_id, device_id, enrollment_nonce):
                home = db.execute("SELECT data FROM homes WHERE id=?", (home_id,)).fetchone()
                return self.device_for(json.loads(home["data"]), device_id)
            for existing_transport, table in BINDING_TABLES.items():
                existing_id = existing_transport + "-" + physical_id
                claim = self.lookup_binding(db, table, existing_id)
                require(claim is None or claim["home_id"] == home_id, 409, "device-owned")
            home = db.execute("SELECT data FROM homes WHERE id=?", (home_id,)).fetchone()
            require(home is not None, 404, "invalid-home")
            data = json.loads(home["data"])
            require(any(room["id"] == room_id for room in data["rooms"]), 400, "invalid-room")
            new_data = clean_home({
                "rooms": data["rooms"], "devices": [{
                    "id": device_id, "roomId": room_id, "transport": transport,
                    "name": manifest.get("name"), "kind": manifest.get("kind"),
                    "capabilities": manifest.get("capabilities"), "state": state
                }]
            })
            device = new_data["devices"][0]
            if online:
                device.update(online=True, lastSeen=int(time.time() * 1000))
            for existing_transport, table in BINDING_TABLES.items():
                if existing_transport != transport:
                    existing_id = existing_transport + "-" + physical_id
                    db.execute(f"DELETE FROM {table} WHERE device_id=? AND home_id=?", (existing_id, home_id))
                    data["devices"] = [item for item in data["devices"] if item["id"] != existing_id]
            old = next((item for item in data["devices"] if item["id"] == device_id), None)
            if old is None:
                require(len(data["devices"]) < 300, 400, "invalid-home-data")
                data["devices"].append(device)
            else:
                if "schedule" in old and has_schedule_power(device["capabilities"]):
                    device["schedule"] = old["schedule"]
                data["devices"][data["devices"].index(old)] = device
            table = BINDING_TABLES[transport]
            db.execute(f"INSERT INTO {table} VALUES (?,?) ON CONFLICT (device_id) DO NOTHING", (device_id, home_id))
            if enrollment_nonce is not None:
                db.execute("INSERT INTO cloud_enrollment_uses VALUES (?,?,?,?)",
                           (device_id, enrollment_nonce, home_id, device_id))
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
            if not schedule or not device["online"] or not has_schedule_power(device["capabilities"]):
                return False
            if power is not None:
                if device.get("transport") or type(power) is not bool:
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
        db.execute("UPDATE homes SET revision=revision+1 WHERE id=?", (home_id,))
        return self.home_snapshot(db, self.membership(db, user, home_id), user)

    def revoke(self, db, user, body):
        home = self.membership(db, user, body.get("homeId"), owner=True)
        user_id = body.get("userId")
        require(identifier(user_id) and user_id != user["id"], 400, "invalid-member")
        deleted = db.execute("DELETE FROM members WHERE home_id=? AND user_id=? AND role='member'",
                             (home["id"], user_id))
        if deleted.rowcount:
            db.execute("UPDATE homes SET revision=revision+1 WHERE id=?", (home["id"],))
        return self.home_snapshot(db, self.membership(db, user, home["id"]), user)

    def member_prefix(self, db, user, body):
        home = self.membership(db, user, body.get("homeId"), owner=True)
        member_id = body.get("userId")
        require(identifier(member_id), 400, "invalid-member")
        member = db.execute("SELECT 1 FROM members WHERE home_id=? AND user_id=?",
                            (home["id"], member_id)).fetchone()
        require(member is not None, 404, "invalid-member")
        prefix = body.get("prefix")
        require(short_text(prefix, 24) and not any(ord(char) < 32 for char in prefix),
                400, "invalid-member-prefix")
        prefix = prefix.strip()
        if prefix:
            db.execute(
                "INSERT INTO member_prefixes VALUES (?,?,?) "
                "ON CONFLICT (home_id,user_id) DO UPDATE SET prefix=excluded.prefix",
                (home["id"], member_id, prefix)
            )
        else:
            db.execute("DELETE FROM member_prefixes WHERE home_id=? AND user_id=?", (home["id"], member_id))
        db.execute("UPDATE homes SET revision=revision+1 WHERE id=?", (home["id"],))
        return self.home_snapshot(db, self.membership(db, user, home["id"]), user)
