"""Local Web Push storage and delivery. The caller authorizes home membership."""
import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Lock
import time
from urllib.parse import urlsplit


DEFAULT_SUBJECT = "https://nexus-home-assistant.onrender.com/"
KEY = re.compile(r"[A-Za-z0-9_-]+={0,2}")
EVENT = re.compile(r"[A-Za-z0-9_.:-]{1,180}")
MAX_SUBSCRIPTIONS = 12
PUSH_HOSTS = {
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "push.services.mozilla.com",
}


class PushUnavailable(RuntimeError):
    """The optional Web Push libraries are unavailable."""


def endpoint_url(value):
    if not isinstance(value, str) or not 0 < len(value) <= 2048 or not value.isascii():
        raise ValueError("push-endpoint")
    if any(ord(letter) <= 32 or ord(letter) == 127 for letter in value) or "\\" in value:
        raise ValueError("push-endpoint")
    parts = urlsplit(value)
    host = parts.hostname or ""
    supported = host in PUSH_HOSTS or host.endswith((".push.apple.com", ".notify.windows.com"))
    if (parts.scheme != "https" or not supported or parts.netloc != host or
            not parts.path.startswith("/") or parts.path == "/" or parts.fragment):
        raise ValueError("push-endpoint")
    return value


def key_bytes(value, size):
    if not isinstance(value, str) or len(value) > 100 or not KEY.fullmatch(value):
        raise ValueError("push-key")
    result = base64.urlsafe_b64decode(value.rstrip("=") + "=" * (-len(value.rstrip("=")) % 4))
    if len(result) != size:
        raise ValueError("push-key")
    return result


def encode_key(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def normalize_subscription(value):
    if not isinstance(value, dict) or not isinstance(value.get("keys"), dict):
        raise ValueError("push-subscription")
    endpoint = endpoint_url(value.get("endpoint"))
    public_key = key_bytes(value["keys"].get("p256dh"), 65)
    if public_key[0] != 4:
        raise ValueError("push-key")
    auth = key_bytes(value["keys"].get("auth"), 16)
    return {"endpoint": endpoint, "keys": {"p256dh": encode_key(public_key), "auth": encode_key(auth)}}


def generate_keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    return (
        encode_key(private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )),
        encode_key(private.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()
        )),
    )


def send_webpush(subscription, payload, private, subject):
    from pywebpush import WebPushException, webpush
    from requests import Session

    class NoRedirectSession(Session):
        def request(self, method, url, **options):
            options["allow_redirects"] = False
            return super().request(method, url, **options)

    try:
        with NoRedirectSession() as session:
            response = webpush(
                subscription_info=subscription, data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=private, vapid_claims={"sub": subject}, ttl=120,
                timeout=5, requests_session=session
            )
        return 200 <= response.status_code < 300
    except WebPushException as error:
        if error.response is not None and error.response.status_code in (404, 410):
            return None
        return False
    except (OSError, ValueError):
        return False


class PushStore:
    """Store VAPID keys and account-scoped subscriptions on the preview server."""

    def __init__(self, path, *, sender=None, key_generator=None, subject=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sender = sender or send_webpush
        self.key_generator = key_generator or generate_keys
        self.injected = sender is not None and key_generator is not None
        self.subject = subject or os.getenv("KONTUR_VAPID_SUBJECT") or DEFAULT_SUBJECT
        self.send_lock = Lock()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS web_push_vapid (
                    id INTEGER PRIMARY KEY CHECK (id = 1), public TEXT NOT NULL, private TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                    user_id TEXT NOT NULL, home_id TEXT NOT NULL, endpoint TEXT NOT NULL,
                    subscription TEXT NOT NULL, updated_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id, home_id, endpoint)
                );
                CREATE INDEX IF NOT EXISTS web_push_subscriptions_home
                    ON web_push_subscriptions(home_id);
                CREATE TABLE IF NOT EXISTS web_push_deliveries (
                    home_id TEXT NOT NULL, event_key TEXT NOT NULL, endpoint TEXT NOT NULL,
                    delivered_at INTEGER NOT NULL, PRIMARY KEY(home_id, event_key, endpoint)
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def available(self):
        return self.injected or all(importlib.util.find_spec(module) is not None
                                    for module in ("pywebpush", "cryptography", "requests"))

    def _keys(self):
        if not self.available():
            raise PushUnavailable("push-dependencies")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            keys = db.execute("SELECT public, private FROM web_push_vapid WHERE id = 1").fetchone()
            if keys is None:
                keys = self.key_generator()
                db.execute("INSERT INTO web_push_vapid VALUES (1, ?, ?)", keys)
        return keys

    def public_key(self):
        return self._keys()[0]

    def subscribe(self, user_id, home_id, subscription):
        if not self.available():
            raise PushUnavailable("push-dependencies")
        valid = normalize_subscription(subscription)
        endpoint = valid["endpoint"]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # One browser subscription must not receive notifications for a previous login.
            previous = db.execute(
                "SELECT user_id, subscription FROM web_push_subscriptions WHERE endpoint = ?", (endpoint,)
            ).fetchall()
            for owner, saved in previous:
                if owner != user_id and json.loads(saved)["keys"] != valid["keys"]:
                    raise ValueError("push-subscription")
            db.execute("DELETE FROM web_push_subscriptions WHERE endpoint = ? AND user_id != ?", (endpoint, user_id))
            db.execute(
                """INSERT INTO web_push_subscriptions VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, home_id, endpoint) DO UPDATE SET
                       subscription = excluded.subscription, updated_at = excluded.updated_at""",
                (user_id, home_id, endpoint, json.dumps(valid), time.time_ns())
            )
            db.execute(
                """DELETE FROM web_push_subscriptions WHERE user_id = ? AND home_id = ? AND endpoint IN (
                       SELECT endpoint FROM web_push_subscriptions WHERE user_id = ? AND home_id = ?
                       ORDER BY updated_at DESC LIMIT -1 OFFSET ?
                   )""",
                (user_id, home_id, user_id, home_id, MAX_SUBSCRIPTIONS)
            )
        return True

    def unsubscribe(self, user_id, home_id, endpoint):
        endpoint = endpoint_url(endpoint)
        with self.connection() as db:
            return bool(db.execute(
                "DELETE FROM web_push_subscriptions WHERE user_id = ? AND home_id = ? AND endpoint = ?",
                (user_id, home_id, endpoint)
            ).rowcount)

    def revoke(self, user_id, home_id):
        with self.connection() as db:
            db.execute("DELETE FROM web_push_subscriptions WHERE user_id = ? AND home_id = ?", (user_id, home_id))

    def unsubscribe_browser(self, user_id, endpoint):
        """Remove this account's subscriptions for the browser logging out."""
        endpoint = endpoint_url(endpoint)
        with self.connection() as db:
            db.execute("DELETE FROM web_push_subscriptions WHERE user_id = ? AND endpoint = ?", (user_id, endpoint))

    def publish(self, home_id, event_key, title, body, device_id=None, simulated=False, allowed=None):
        if (not isinstance(event_key, str) or not EVENT.fullmatch(event_key)
                or not isinstance(title, str) or not 0 < len(title) <= 100
                or not isinstance(body, str) or not 0 < len(body) <= 300
                or (device_id is not None and (not isinstance(device_id, str) or len(device_id) > 128))):
            raise ValueError("push-event")
        tag = hashlib.sha256(f"{home_id}:{event_key}".encode()).hexdigest()[:24]
        payload = {"title": ("[TEST] " if simulated else "") + title, "body": body, "tag": tag,
                   "deviceId": device_id, "homeId": home_id}
        result = {"sent": 0, "failed": 0, "duplicate": 0}
        with self.send_lock:
            with self.connection() as db:
                subscribers = db.execute(
                    "SELECT user_id, endpoint, subscription FROM web_push_subscriptions WHERE home_id = ?", (home_id,)
                ).fetchall()
            if not subscribers:
                return result
            private = self._keys()[1]
            for user_id, endpoint, subscription in subscribers:
                if allowed is not None and not allowed(user_id, home_id):
                    self.revoke(user_id, home_id)
                    continue
                recipient_hash = hashlib.sha256(endpoint.encode()).hexdigest()
                with self.connection() as db:
                    delivered = db.execute(
                        "SELECT 1 FROM web_push_deliveries WHERE home_id = ? AND event_key = ? AND endpoint = ?",
                        (home_id, event_key, recipient_hash)
                    ).fetchone()
                    current = db.execute(
                        """SELECT 1 FROM web_push_subscriptions
                           WHERE user_id = ? AND home_id = ? AND endpoint = ? AND subscription = ?""",
                        (user_id, home_id, endpoint, subscription)
                    ).fetchone()
                if not current:
                    continue
                if delivered:
                    result["duplicate"] += 1
                    continue
                outcome = self.sender(json.loads(subscription), payload, private, self.subject)
                if outcome is True:
                    with self.connection() as db:
                        db.execute("INSERT INTO web_push_deliveries VALUES (?, ?, ?, ?)",
                                   (home_id, event_key, recipient_hash, int(time.time())))
                    result["sent"] += 1
                elif outcome is None:
                    with self.connection() as db:
                        db.execute("DELETE FROM web_push_subscriptions WHERE endpoint = ? AND subscription = ?",
                                   (endpoint, subscription))
                    result["failed"] += 1
                else:
                    result["failed"] += 1
        return result
