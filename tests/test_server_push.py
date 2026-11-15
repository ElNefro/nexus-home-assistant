import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server_push import PushStore, PushUnavailable, encode_key, normalize_subscription
from server_accounts import AccountError
from shared_push_routes import PushRoutes


def subscription(suffix):
    return {
        "endpoint": "https://fcm.googleapis.com/fcm/send/" + suffix,
        "keys": {"p256dh": encode_key(b"\x04" + b"\x03" * 64), "auth": encode_key(b"x" * 16)},
    }


class PushTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "push.sqlite3"
        self.messages = []
        self.results = {}

        def send(receiver, payload, private, subject):
            self.messages.append((receiver, payload, private))
            return self.results.get(receiver["endpoint"], True)

        self.store = PushStore(self.db, sender=send, key_generator=lambda: ("public", "private"))

    def test_test_message_routes_to_device_and_deduplicates_per_home(self):
        for owner, home, suffix in (("owner", "one", "a"), ("member", "one", "b"), ("guest", "two", "c")):
            self.store.subscribe(owner, home, subscription(suffix))
        delivery = self.store.publish("one", "water:42", "Бак", "Закончилась вода", "lamp", simulated=True)
        self.assertEqual(delivery, {"sent": 2, "failed": 0, "duplicate": 0})
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0][1]["deviceId"], "lamp")
        self.assertEqual(self.messages[0][1]["title"], "[TEST] Бак")
        self.assertEqual(self.store.public_key(), "public")
        self.assertEqual(self.store.publish("one", "water:42", "Бак", "Закончилась вода")["duplicate"], 2)
        self.assertEqual(self.store.publish("two", "water:42", "Бак", "Закончилась вода")["sent"], 1)

    def test_expired_endpoint_is_removed_and_failed_delivery_can_retry(self):
        expired, temporary = subscription("expired"), subscription("temporary")
        self.store.subscribe("owner", "home", expired)
        self.store.subscribe("owner", "home", temporary)
        self.results[expired["endpoint"]] = None
        self.results[temporary["endpoint"]] = False
        self.assertEqual(self.store.publish("home", "event", "Title", "Body")["failed"], 2)
        self.results[temporary["endpoint"]] = True
        self.assertEqual(self.store.publish("home", "event", "Title", "Body")["sent"], 1)
        self.assertEqual(len(self.messages), 3)

    def test_account_switch_and_membership_guard_exclude_previous_recipients(self):
        browser = subscription("browser")
        self.store.subscribe("old", "one", browser)
        self.store.subscribe("new", "two", browser)
        self.assertEqual(self.store.publish("one", "event", "Title", "Body")["sent"], 0)
        self.assertEqual(self.store.publish("two", "event", "Title", "Body", allowed=lambda *_: False)["sent"], 0)
        self.assertEqual(self.store.publish("two", "event", "Title", "Body")["sent"], 0)
        self.assertFalse(self.messages)

    def test_explicit_unsubscribe_only_removes_requested_home_or_browser(self):
        browser = subscription("browser")
        second = subscription("second")
        self.store.subscribe("owner", "one", browser)
        self.store.subscribe("owner", "two", browser)
        self.store.subscribe("owner", "two", second)
        self.assertTrue(self.store.unsubscribe("owner", "two", browser["endpoint"]))
        self.assertEqual(self.store.publish("one", "event", "Title", "Body")["sent"], 1)
        self.assertEqual(self.store.publish("two", "event", "Title", "Body")["sent"], 1)
        self.store.unsubscribe_browser("owner", browser["endpoint"])
        self.assertEqual(self.store.publish("one", "next", "Title", "Body")["sent"], 0)

    def test_key_persists_and_missing_dependencies_do_not_break_server(self):
        self.assertEqual(self.store.public_key(), "public")
        reopened = PushStore(self.db, sender=lambda *_: True, key_generator=lambda: ("wrong", "wrong"))
        self.assertEqual(reopened.public_key(), "public")
        optional = PushStore(Path(self.directory.name) / "optional.sqlite3")
        with patch("server_push.importlib.util.find_spec", return_value=None):
            self.assertFalse(optional.available())
            with self.assertRaises(PushUnavailable):
                optional.public_key()

    def test_validation_restricts_endpoints_and_key_lengths(self):
        base = subscription("safe")
        for endpoint in (
            "http://fcm.googleapis.com/fcm/send/a",
            "https://127.0.0.1/secret",
            "https://fcm.googleapis.com.evil.invalid/secret",
            "https://fcm.googleapis.com:444/path",
            "https://fcm.googleapis.com@127.0.0.1/secret",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                normalize_subscription({**base, "endpoint": endpoint})
        with self.assertRaises(ValueError):
            normalize_subscription({**base, "keys": {**base["keys"], "auth": "tiny"}})
        self.assertEqual(normalize_subscription(base), base)

    def test_routes_enforce_membership_and_owner_for_explicit_test_push(self):
        accounts = TestAccounts()
        routes = PushRoutes(self.db, accounts, store=self.store)
        home = {"homeId": "home"}
        self.assertEqual(routes.route("POST", "/api/push/key", home, None)[0], 401)
        self.assertEqual(routes.route("POST", "/api/push/key", home, "owner")[1], {"publicKey": "public"})
        body = {**home, "subscription": subscription("member")}
        self.assertEqual(routes.route("POST", "/api/push/subscribe", body, "member")[0], 200)
        self.assertEqual(routes.route("POST", "/api/push/test", home, "member")[0], 403)
        self.assertEqual(routes.route("POST", "/api/push/test", {**home, "deviceId": "other"}, "owner")[0], 404)
        self.assertEqual(routes.route("POST", "/api/push/test", home, "owner")[1]["sent"], 1)
        self.assertEqual(self.messages[0][1]["deviceId"], "humidifier")
        self.assertEqual(self.messages[0][1]["homeId"], "home")
        accounts.member = False
        self.assertEqual(routes.route("POST", "/api/push/key", home, "member")[0], 403)
        self.assertEqual(self.store.publish("home", "next", "Title", "Body", allowed=accounts.has_membership)["sent"], 0)

    def test_water_empty_fires_once_until_refill_and_survives_restart(self):
        self.store.subscribe("owner","home",subscription("owner"))
        accounts = TestAccounts()
        routes = PushRoutes(self.db, accounts, store=self.store)
        device = {"kind": "humidifier", "transport": "lan", "online": True, "id": "humidifier", "name": "Увлажнитель"}
        empty = {**device, "state": {"water_level": 0}}
        self.assertEqual(routes.observe("home", empty)["sent"], 1)
        restarted = PushRoutes(self.db, accounts, store=self.store)
        self.assertIsNone(restarted.observe("home", empty))
        self.assertIsNone(restarted.observe("home",{**device,"online": False,"state": {"water_level": 20}}))
        self.assertIsNone(restarted.observe("home", empty))
        restarted.observe("home", {**device, "state": {"water_level": 22}})
        self.assertEqual(restarted.observe("home", empty)["sent"], 1)
        self.assertEqual(len(self.messages), 2)

    def test_water_delivery_retries_only_for_unsent_subscribers(self):
        owner, member = subscription("owner"), subscription("member")
        self.store.subscribe("owner", "home", owner)
        self.store.subscribe("member", "home", member)
        self.results[member["endpoint"]] = False
        accounts = TestAccounts()
        routes = PushRoutes(self.db, accounts, store=self.store)
        empty = {"kind": "humidifier", "transport": "lan", "online": True, "id": "humidifier",
                 "state": {"water_level": 0}}
        with patch("shared_push_routes.time.time", return_value=100):
            self.assertEqual(routes.observe("home",empty), {"sent": 1, "failed": 1, "duplicate": 0})
        self.results[member["endpoint"]] = True
        restarted = PushRoutes(self.db, accounts, store=self.store)
        with patch("shared_push_routes.time.time", return_value=114):
            self.assertIsNone(restarted.observe("home",empty))
        with patch("shared_push_routes.time.time", return_value=115):
            self.assertEqual(restarted.observe("home",empty), {"sent": 1, "failed": 0, "duplicate": 1})
        self.assertEqual(len(self.messages),3)


class TestAccounts:
    member = True

    def has_membership(self, user_id, home_id):
        return home_id == "home" and (user_id == "owner" or user_id == "member" and self.member)

    def authorize(self, cookie, home_id, owner=False):
        if cookie is None:
            raise AccountError(401, "sign-in-required")
        if not self.has_membership(cookie, home_id) or owner and cookie != "owner":
            raise AccountError(403, "home-access-denied")
        return {"userId": cookie, "homeId": home_id}

    def route(self, method, path, body, cookie):
        self.authorize(cookie, body["homeId"])
        return 200, {"state": {"devices": [{"id": "humidifier", "kind": "humidifier"}]}}, {}


if __name__ == "__main__":
    unittest.main()
