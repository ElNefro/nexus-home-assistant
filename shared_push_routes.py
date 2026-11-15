"""Account authorization for Web Push and persisted water-level warning episodes."""
import hashlib
import time

from server_accounts import AccountError
from server_push import PushStore, PushUnavailable


ROUTES = {"/api/push/key", "/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test"}
EMPTY_BODY = "В баке закончилась вода."
TEST_BODY = "Имитация. В баке увлажнителя закончилась вода."
DELIVERY_RETRY_SECONDS = (15, 60, 300)


class PushRoutes:
    def __init__(self, path, accounts, *, store=None):
        self.accounts = accounts
        self.store = store or PushStore(path)
        with self.store.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS web_push_water_episodes (
                    home_id TEXT NOT NULL, device_id TEXT NOT NULL,
                    empty INTEGER NOT NULL, episode INTEGER NOT NULL,
                    PRIMARY KEY(home_id, device_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS web_push_water_retries (
                    home_id TEXT NOT NULL, device_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL, due INTEGER NOT NULL,
                    PRIMARY KEY(home_id, device_id)
                )
            """)

    def route(self, method, path, body, cookie):
        if path not in ROUTES:
            return None
        if method != "POST":
            return 405, {"error": "method-not-allowed"}, {"Allow": "POST"}
        if not isinstance(body, dict):
            return 400, {"error": "invalid-request"}, {}
        try:
            auth = self.accounts.authorize(cookie, body.get("homeId"), owner=(path == "/api/push/test"))
            user, home = auth["userId"], auth["homeId"]
            if path == "/api/push/key":
                return 200, {"publicKey": self.store.public_key()}, {}
            if path == "/api/push/subscribe":
                self.store.subscribe(user, home, body.get("subscription"))
                return 200, {"subscribed": True}, {}
            if path == "/api/push/unsubscribe":
                self.store.unsubscribe(user, home, body.get("endpoint"))
                return 200, {"subscribed": False}, {}
            device = self.test_device(cookie, home, body.get("deviceId"))
            result = self.store.publish(
                home, f"water-test:{int(time.time()) // 30}", "Контур", TEST_BODY,
                device_id=device, simulated=True, allowed=self.accounts.has_membership
            )
            return 200, result, {}
        except AccountError as error:
            return error.status, {"error": error.code}, {}
        except PushUnavailable:
            return 503, {"error": "push-unavailable"}, {}
        except ValueError:
            return 400, {"error": "invalid-push"}, {}

    def test_device(self, cookie, home, requested):
        status, snapshot, _ = self.accounts.route("POST", "/api/home/get", {"homeId": home}, cookie)
        if status != 200:
            raise AccountError(status, snapshot["error"])
        humidifiers = [device for device in snapshot["state"]["devices"] if device["kind"] == "humidifier"]
        if requested is None:
            return humidifiers[0]["id"] if humidifiers else None
        if not any(device["id"] == requested for device in humidifiers):
            raise AccountError(404, "device-not-found")
        return requested

    def observe(self, home_id, device):
        """Observe trusted hardware readings. Run from the server worker, away from HTTP UI requests."""
        if (device.get("kind") != "humidifier" or not device.get("transport") or
                device.get("online") is not True or not self.store.available()):
            return
        water = device.get("state", {}).get("water_level")
        if type(water) not in (int, float) or not 0 <= water <= 100:
            return
        device_id = device["id"]
        empty = int(water == 0)
        now = int(time.time())
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute(
                "SELECT empty, episode FROM web_push_water_episodes WHERE home_id = ? AND device_id = ?",
                (home_id, device_id)
            ).fetchone()
            episode = saved[1] if saved else 0
            if saved is None or saved[0] != empty:
                episode += empty
                db.execute(
                    """INSERT INTO web_push_water_episodes VALUES (?, ?, ?, ?)
                       ON CONFLICT(home_id, device_id) DO UPDATE SET empty = excluded.empty, episode = excluded.episode""",
                    (home_id, device_id, empty, episode)
                )
                db.execute("DELETE FROM web_push_water_retries WHERE home_id = ? AND device_id = ?",
                           (home_id, device_id))
                if empty:
                    db.execute("INSERT INTO web_push_water_retries VALUES (?, ?, 0, ?)", (home_id, device_id, now))
            if not empty:
                return
            retry = db.execute(
                "SELECT attempt, due FROM web_push_water_retries WHERE home_id = ? AND device_id = ?",
                (home_id, device_id)
            ).fetchone()
            if retry is None or retry[1] > now:
                return
            attempt = retry[0]
            if attempt >= len(DELIVERY_RETRY_SECONDS):
                db.execute("DELETE FROM web_push_water_retries WHERE home_id = ? AND device_id = ?",
                           (home_id, device_id))
            else:
                db.execute(
                    "UPDATE web_push_water_retries SET attempt = ?, due = ? WHERE home_id = ? AND device_id = ?",
                    (attempt + 1, now + DELIVERY_RETRY_SECONDS[attempt], home_id, device_id)
                )

        signature = hashlib.sha256(device_id.encode()).hexdigest()
        result = self.store.publish(
            home_id, f"water-empty:{signature}:{episode}", device.get("name") or "Контур", EMPTY_BODY,
            device_id=device_id, simulated=device.get("simulated") is True, allowed=self.accounts.has_membership
        )
        if not result["failed"]:
            with self.store.connection() as db:
                db.execute(
                    """DELETE FROM web_push_water_retries WHERE home_id = ? AND device_id = ?
                       AND attempt = ? AND EXISTS (
                         SELECT 1 FROM web_push_water_episodes
                         WHERE home_id = ? AND device_id = ? AND episode = ? AND empty = 1
                       )""",
                    (home_id, device_id, attempt + 1, home_id, device_id, episode)
                )
        return result

    def after_revoke(self, user_id, home_id):
        self.store.revoke(user_id, home_id)
