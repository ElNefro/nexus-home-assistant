import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from local_server import Handler, SharedDevices
from server_accounts import Accounts
from server_automation import decision


class PreviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.server.accounts = Accounts(Path(self.temp.name) / "home.sqlite3")
        self.server.push = None
        self.server.shared_devices = SharedDevices(self.server.accounts,None)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, body, cookie="", origin="http://localhost:8080"):
        connection = HTTPConnection(*self.server.server_address,timeout=5)
        try:
            connection.request("POST",path,json.dumps(body),{
                "Content-Type":"application/json","Host":"localhost:8080","Origin":origin,"Cookie":cookie
            })
            response = connection.getresponse()
            return response.status,json.loads(response.read()),response.getheader("Set-Cookie")
        finally:
            connection.close()

    def register(self, email):
        code, user, cookie = self.post("/api/account/register",{
            "email":email,"name":email.split("@")[0],"password":"preview-validation-123"
        })
        self.assertEqual(code,201)
        self.assertIn("HttpOnly",cookie)
        return user,cookie.split(";")[0]

    def test_shared_home_over_http(self):
        _,owner = self.register("owner@example.test")
        _,member = self.register("member@example.test")
        room = {"id":"living","name":"Living room"}
        switch = {"id":"power","type":"switch","label":"Power"}
        light = {"id":"lamp","name":"Lamp","kind":"light","roomId":"living","online":True,
                 "capabilities":[switch],"state":{"power":False}}
        code, home, _ = self.post("/api/home/create",{"name":"Test home","state":{
            "rooms":[room],"devices":[light],"roomId":"living"
        }},owner)
        self.assertIn(code,(200,201))
        home_id = home["id"]
        _, invite, _ = self.post("/api/home/invite",{"homeId":home_id},owner)
        code, joined, _ = self.post("/api/home/join",invite,member)
        self.assertEqual(code,200)
        command = {"homeId":home_id,"id":"lamp","capability":"power","value":True}
        code, _, _ = self.post("/api/home/command",command,member,origin="https://unrelated.example")
        self.assertEqual(code,403)
        code, updated, _ = self.post("/api/home/command",command,member)
        self.assertEqual(code,200)
        self.assertTrue(updated["state"]["devices"][0]["state"]["power"])
        _, viewed, _ = self.post("/api/home/get",{"homeId":home_id},owner)
        self.assertEqual(viewed["revision"],updated["revision"])
        target = next(person["id"] for person in joined["members"] if person["role"] == "member")
        self.assertEqual(self.post("/api/home/revoke",{"homeId":home_id,"userId":target},owner)[0],200)
        self.assertEqual(self.post("/api/home/command",command,member)[0],403)
        self.assertEqual(self.post("/api/home/get",{"homeId":home_id},member)[0],403)

    def test_background_rule_uses_room_sensor(self):
        sensor = {"roomId":"living","online":True,"capabilities":[{"id":"air_quality","type":"sensor"}],
                  "state":{"air_quality":62}}
        purifier = {"kind":"purifier","roomId":"living","online":True,
                    "capabilities":[{"id":"power","type":"switch"}],"state":{"power":False},
                    "schedule":{"mode":"auto","threshold":80}}
        self.assertEqual(decision(purifier,[sensor],180000,None),(True,None))
        sensor["roomId"] = "kitchen"
        self.assertEqual(decision(purifier,[sensor],180000,None),(None,None))


if __name__ == "__main__":
    unittest.main()
