import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import local_server as bridge


MANIFEST = {
    "schemaVersion": 1, "id": "light-0123456789ab", "name": "Светильник ESP32", "kind": "light",
    "requiresToken": True, "capabilities": [
        {"id":"power", "type":"switch", "label":"Питание"},
        {"id":"brightness", "type":"slider", "label":"Яркость", "min":0, "max":100, "step":1},
    ],
}

IR_MANIFEST = {
    "schemaVersion": 1, "id": "ir-projector-0123456789ab", "name": "BL-XK01", "kind": "light",
    "requiresToken": True, "commandFeedback": "transmitted",
    "description": "ИК-пульт передаёт команды, но не получает состояние проектора.",
    "capabilities": [
        {"id": action, "type": "switch", "label": action, "momentary": True,
         **({"description": "Сначала включите синий и красный свет."} if action == "purple" else {})}
        for action in ("toggle", "brightness", "white", "red", "green", "blue", "purple", "rotation")
    ],
}


class Lamp(BaseHTTPRequestHandler):
    state = {"power": False, "brightness": 50}

    def log_message(self, *_):
        pass

    def reply(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        if self.path == "/kontur/v1/manifest":
            self.reply(200, MANIFEST)
        elif self.headers.get("Authorization") == "Bearer local-test":
            self.reply(200, self.state)
        else:
            self.reply(401, {})

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer local-test":
            self.reply(401, {})
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.state[body["capability"]] = body["value"]
        self.reply(200, self.state)


class BridgeTest(unittest.TestCase):
    def test_external_click_can_open_dashboard_without_exposing_local_api(self):
        headers = {
            "Host": "localhost:8080", "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-User": "?1",
        }

        with ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(method, path, request_headers, body=None):
                    conn = http.client.HTTPConnection(*server.server_address, timeout=2)
                    try:
                        conn.request(method, path, body=body, headers=request_headers)
                        res = conn.getresponse()
                        return res.status, res.read()
                    finally:
                        conn.close()

                code, dashboard = request("GET", "/", headers)
                self.assertEqual(code, 200)
                self.assertIn(b"./app.js", dashboard)
                self.assertEqual(request("GET", "/api/discovery", headers)[0], 403)
                self.assertEqual(request("GET", "/", {**headers, "Sec-Fetch-Dest": "iframe"})[0], 403)
                self.assertEqual(request("GET", "/", {**headers, "Host": "example.com"})[0], 403)
                self.assertEqual(request("POST", "/api/command", {
                    **headers, "Origin": "https://nexus-home-assistant.onrender.com",
                    "Content-Type": "application/json",
                }, "{}")[0], 403)
            finally:
                server.shutdown()
                thread.join()

    def test_pair_read_command_and_rejection(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), Lamp) as lamp:
            thread = threading.Thread(target=lamp.serve_forever,daemon=True)
            thread.start()
            try:
                registry = bridge.Registry()
                with patch.object(bridge, "discover", return_value=[(MANIFEST, lamp.server_address)]):
                    self.assertEqual(registry.scan(),[MANIFEST])
                with self.assertRaises(PermissionError):
                    registry.pair(MANIFEST["id"],"wrong")
                connection = registry.pair(MANIFEST["id"],"local-test")
                self.assertEqual(connection["state"], {"power":False,"brightness":50})
                session = connection["session"]
                with self.assertRaises(ValueError):
                    registry.session(session,{"capability":"brightness","value":101})
                result = registry.session(session,{"capability":"brightness","value":80})
                self.assertEqual(result["state"]["brightness"],80)
                self.assertEqual(registry.session(session),result)
            finally:
                lamp.shutdown()
                thread.join()

    def test_manifest_validation(self):
        self.assertEqual(bridge.normalize_manifest(MANIFEST),MANIFEST)
        duplicate = {**MANIFEST, "capabilities":[MANIFEST["capabilities"][0]]*2}
        with self.assertRaises(ValueError):
            bridge.normalize_manifest(duplicate)

    def test_password_device_pair_read_and_repeated_actions_use_password_header(self):
        manifest = {**IR_MANIFEST, "requiresToken": False, "requiresPassword": True}
        requests = []
        actions = []

        class PasswordProjector(Lamp):
            def authorized(self):
                requests.append((self.headers.get("Authorization"), self.headers.get("X-Kontur-Password")))
                return self.headers.get("X-Kontur-Password") == "11111111"

            def do_GET(self):
                if self.path == "/kontur/v1/manifest":
                    self.reply(200, manifest)
                elif self.authorized():
                    self.reply(200, {})
                else:
                    self.reply(401, {})

            def do_POST(self):
                if not self.authorized():
                    self.reply(401, {})
                    return
                command = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                actions.append(command)
                self.reply(200, {command["capability"]: command["value"]})

        with ThreadingHTTPServer(("127.0.0.1", 0), PasswordProjector) as device:
            thread = threading.Thread(target=device.serve_forever, daemon=True)
            thread.start()
            try:
                registry = bridge.Registry()
                with patch.object(bridge, "discover", return_value=[(manifest, device.server_address)]):
                    self.assertEqual(registry.scan(), [manifest])
                for secret in ("", "wrong-password"):
                    with self.assertRaises(PermissionError):
                        registry.pair(manifest["id"], secret)
                with self.assertRaises(PermissionError):
                    bridge.device_request(device.server_address, "state", token="11111111")
                pair = registry.pair(manifest["id"], "11111111")
                self.assertEqual(actions, [])
                self.assertTrue(pair["manifest"]["requiresPassword"])
                self.assertFalse(pair["manifest"]["requiresToken"])
                self.assertEqual(registry.session(pair["session"])["state"], dict.fromkeys(
                    (cap["id"] for cap in manifest["capabilities"]), False))
                action = {"capability": "green", "value": True}
                for _ in range(2):
                    self.assertEqual(registry.session(pair["session"], action)["state"], {"green": True})
                self.assertEqual(actions, [action, action])
                self.assertTrue(all(auth is None and password == "11111111" for auth, password in requests[-4:]))
            finally:
                device.shutdown()
                thread.join()

    def test_password_manifest_requires_explicit_unambiguous_authentication(self):
        manifest = {**MANIFEST, "requiresToken": False, "requiresPassword": True, "pairSignal": True}
        self.assertEqual(bridge.normalize_manifest(manifest), manifest)
        for properties in ({"requiresPassword": True}, {"requiresPassword": "true"},
                           {"requiresPassword": None}, {"requiresToken": True, "requiresPassword": True}):
            with self.subTest(properties=properties), self.assertRaises(ValueError):
                bridge.normalize_manifest({**MANIFEST, **properties})

    def test_ir_metadata_discovery_pair_and_shared_command_remain_transient(self):
        raw_state = {cap["id"]: True for cap in IR_MANIFEST["capabilities"]}
        idle = dict.fromkeys(raw_state, False)
        registry = bridge.Registry()
        address = ("192.168.1.50", 80)
        with patch.object(bridge, "device_request", return_value=IR_MANIFEST):
            discovered = bridge.resolve_broadcast(address)
        self.assertEqual(discovered, (IR_MANIFEST, address))
        with patch.object(bridge, "discover", return_value=[discovered]):
            self.assertEqual(registry.scan(), [IR_MANIFEST])

        def response(_address, route, command=None, token=None):
            self.assertEqual(_address, address)
            if route == "manifest":
                return IR_MANIFEST
            self.assertEqual(token, "local-test")
            if command:
                return {command["capability"]: True}
            return raw_state

        with patch.object(bridge, "device_request", side_effect=response), \
                patch.object(bridge, "REGISTRY", registry), tempfile.TemporaryDirectory() as folder:
            pair = registry.pair(IR_MANIFEST["id"], "local-test")
            self.assertEqual(pair["manifest"], IR_MANIFEST)
            self.assertEqual(pair["state"], idle)
            action = {"capability": "purple", "value": True}
            self.assertEqual(registry.session(pair["session"], action)["state"], {"purple": True})
            self.assertEqual(registry.session(pair["session"])["state"], idle)

            accounts = bridge.Accounts(Path(folder) / "shared.db")
            _, _, headers = accounts.route("POST", "/api/account/register", {
                "email": "owner@example.test", "password": "local test password", "name": "Owner"
            }, None)
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            _, home, _ = accounts.route("POST", "/api/home/create", {"name": "Home"}, cookie)
            shared = bridge.SharedDevices(accounts, None)
            shared.add(home["id"], "living", pair)
            device_id = "lan-" + IR_MANIFEST["id"]
            self.assertEqual(shared.read(home["id"], device_id, pair["session"], action), {"purple": True})
            status, snapshot, _ = accounts.route("POST", "/api/home/get", {"homeId": home["id"]}, cookie)
            self.assertEqual(status, 200)
            device = snapshot["state"]["devices"][0]
            self.assertEqual(device["commandFeedback"], "transmitted")
            self.assertEqual(device["description"], IR_MANIFEST["description"])
            self.assertEqual(device["capabilities"], IR_MANIFEST["capabilities"])
            self.assertEqual(device["state"], idle)
            self.assertNotIn("schedule", device)

    def test_invalid_known_ir_metadata_rejects_manifest(self):
        for key, value in (("commandFeedback", "applied"), ("commandFeedback", None),
                           ("description", None), ("description", "x" * 501),
                           ("description", "unsafe\x00text"), ("description", "\ud800")):
            with self.subTest(key=key, value=repr(value)), self.assertRaises(ValueError):
                bridge.normalize_manifest({**IR_MANIFEST, key: value})
        for value in (None, 12, "x" * 501, "unsafe\x00text", "\ud800"):
            manifest = {**IR_MANIFEST, "capabilities": [{**IR_MANIFEST["capabilities"][0], "description": value}]}
            with self.subTest(capability_description=repr(value)), self.assertRaises(ValueError):
                bridge.normalize_manifest(manifest)

    def test_pair_signal_is_authenticated_and_state_reads_do_not_retrigger_it(self):
        manifest = {
            **MANIFEST, "pairSignal": True,
            "capabilities": [{**MANIFEST["capabilities"][0], "momentary": True}],
        }
        signals = []

        class Indicator(Lamp):
            def do_GET(self):
                if self.path == "/kontur/v1/manifest":
                    self.reply(200, manifest)
                else:
                    self.reply(200, {"power": False})

            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer local-test":
                    self.reply(401, {})
                elif self.path == "/kontur/v1/pair":
                    signals.append(1)
                    self.reply(200, {"power": False})
                elif self.path == "/kontur/v1/command":
                    command = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if command == {"capability": "power", "value": True}:
                        signals.append(4)
                        self.reply(200, {"power": True})
                    else:
                        self.reply(400, {})
                else:
                    self.reply(404, {})

        with ThreadingHTTPServer(("127.0.0.1", 0), Indicator) as device:
            thread = threading.Thread(target=device.serve_forever, daemon=True)
            thread.start()
            try:
                registry = bridge.Registry()
                with patch.object(bridge, "discover", return_value=[(manifest, device.server_address)]):
                    registry.scan()
                with self.assertRaises(PermissionError):
                    registry.pair(manifest["id"], "wrong")
                self.assertEqual(signals, [])
                pair = registry.pair(manifest["id"], "local-test")
                self.assertEqual(pair["state"], {"power": False})
                for _ in range(2):
                    self.assertEqual(registry.session(pair["session"])["state"], {"power": False})
                    response = registry.session(pair["session"], {"capability": "power", "value": True})
                    self.assertEqual(response["state"], {"power": True})
                self.assertEqual(signals, [1, 4, 4])
            finally:
                device.shutdown()
                thread.join()

    def test_state_only_exposes_valid_declared_values(self):
        self.assertEqual(bridge.state_for(MANIFEST,{"power":True,"brightness":101,"extra":2}),{"power":True})


if __name__ == "__main__":
    unittest.main()
