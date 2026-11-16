import io
import json
import unittest
from hashlib import sha256
from hmac import digest as hmac_digest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from types import SimpleNamespace

from cloud_server import (Config, Handler, MAX_PENDING, PANEL_ORIGIN, Relay, RelayError,
                          b64url, issue_enrollment_ticket, verify_enrollment_ticket)
from server_accounts import Accounts


DEVICE_ID = "light-0123456789ab"
OWNER = "a" * 43
DEVICE = "b" * 43
SETUP = "c" * 64
MANIFEST = {
    "schemaVersion": 1, "id": DEVICE_ID, "kind": "light", "name": "Test light",
    "capabilities": [{"id": "power", "type": "switch", "label": "Pulse", "momentary": True}]
}


def config_env():
    return {
        "KONTUR_OWNER_SHA256": sha256(OWNER.encode()).hexdigest(),
        "KONTUR_DEVICE_SHA256": sha256(DEVICE.encode()).hexdigest(),
        "KONTUR_DEVICE_ID": DEVICE_ID,
    }


class Connection:
    def __init__(self, request):
        self.incoming = io.BytesIO(request)
        self.outgoing = io.BytesIO()

    def makefile(self, *_):
        return self.incoming

    def sendall(self, data):
        self.outgoing.write(data)


class QuietHandler(Handler):
    def log_message(self, *_):
        pass


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.relay = Relay(DEVICE_ID,lambda: self.now)

    def poll(self, results=None):
        return self.relay.poll({"manifest": MANIFEST, "results": results or []})["command"]

    def command(self):
        return self.relay.enqueue({"deviceId":DEVICE_ID,"capability":"power","value":True})["id"]

    def test_configuration_requires_separate_hashes_and_exact_origin(self):
        with self.assertRaises(ValueError):
            Config.from_env({})
        equal_hashes = config_env()
        equal_hashes["KONTUR_DEVICE_SHA256"] = equal_hashes["KONTUR_OWNER_SHA256"]
        with self.assertRaises(ValueError):
            Config.from_env(equal_hashes)
        with self.assertRaises(ValueError):
            Config.from_env({**config_env(),"KONTUR_PANEL_ORIGIN":"https://example.test/path"})
        self.assertEqual(Config.from_env(config_env()).host,"127.0.0.1")
        self.assertEqual(Config.from_env({**config_env(),"RENDER":"true"}).host,"0.0.0.0")

    def test_same_command_id_retries_and_acknowledgments_only_apply_after_delivery(self):
        self.assertIsNone(self.poll())
        first = self.command()
        second = self.command()
        first_packet = self.poll([{"id":second,"ok":True}])
        self.assertEqual(first_packet,{"id":first,"capability":"power","value":True})
        self.assertEqual(self.relay.status(second),{"status":"pending"})
        self.assertEqual(self.poll(),first_packet)
        self.assertEqual(self.poll([{"id":first,"ok":True}])["id"],second)
        self.assertEqual(self.relay.status(first),{"status":"applied"})
        self.assertIsNone(self.poll([{"id":second,"ok":False}]))
        self.assertEqual(self.relay.status(second),{"status":"failed"})

    def test_offline_expiry_and_queue_bounds(self):
        with self.assertRaisesRegex(RelayError,"device-offline"):
            self.command()
        self.poll()
        first = self.command()
        for _ in range(MAX_PENDING - 1):
            self.command()
        with self.assertRaisesRegex(RelayError,"command-queue-full"):
            self.command()
        self.now = 20
        self.assertFalse(self.relay.devices()["devices"][0]["online"])
        with self.assertRaisesRegex(RelayError,"device-offline"):
            self.command()
        self.now = 30
        self.assertEqual(self.relay.status(first),{"status":"expired"})
        self.assertIsNone(self.poll([{"id":first,"ok":True}]))
        self.now = 211
        with self.assertRaisesRegex(RelayError,"command-not-found"):
            self.relay.status(first)

    def test_only_one_provisioned_light_and_momentary_power(self):
        with self.assertRaisesRegex(RelayError,"invalid-device-manifest"):
            self.relay.poll({"manifest":dict(MANIFEST,id="different-board"),"results":[]})
        sustained = {**MANIFEST,"capabilities":[{**MANIFEST["capabilities"][0],"momentary":False}]}
        with self.assertRaisesRegex(RelayError,"invalid-device-manifest"):
            self.relay.poll({"manifest":sustained,"results":[]})
        self.poll()
        for command in ({"capability":"power","value":False},{"capability":"power","value":1},
                        {"capability":"brightness","value":True}):
            with self.assertRaisesRegex(RelayError,"invalid-command"):
                self.relay.enqueue({"deviceId":DEVICE_ID,**command})

    def request(self, method, path, *, bearer=None, origin=None, body=None, extra=None, server=None):
        headers = ["Host: relay.example.test"]
        if bearer:
            headers.append("Authorization: Bearer " + bearer)
        if origin:
            headers.append("Origin: " + origin)
        if extra:
            headers.extend(extra)
        payload = b""
        if body is not None:
            payload = json.dumps(body).encode()
            headers.append("Content-Type: application/json")
            headers.append("Content-Length: " + str(len(payload)))
        request = "\r\n".join([f"{method} {path} HTTP/1.1",*headers,"",""]).encode() + payload
        connection = Connection(request)
        if server is None:
            server = SimpleNamespace(config=Config.from_env(config_env()),relay=self.relay)
        QuietHandler(connection,("127.0.0.1",1234),server)
        message, content = connection.outgoing.getvalue().split(b"\r\n\r\n",1)
        return int(message.split()[1]),message.decode(),json.loads(content) if content else None

    def test_http_requires_correct_bearer_and_origin_through_entire_cycle(self):
        api = "/api/cloud"
        self.assertEqual(self.request("GET","/healthz")[0],200)
        self.assertEqual(self.request("GET",api + "/devices",bearer=DEVICE)[0],401)
        self.assertEqual(self.request("GET",api + "/devices",bearer=OWNER,origin="https://other.test")[0],403)
        options = [
            "Access-Control-Request-Method: POST",
            "Access-Control-Request-Headers: authorization, content-type"
        ]
        status, headers, _ = self.request("OPTIONS",api + "/commands",origin=PANEL_ORIGIN,extra=options)
        self.assertEqual(status,204)
        self.assertIn("Access-Control-Allow-Origin: " + PANEL_ORIGIN,headers)
        self.assertEqual(self.request("OPTIONS",api + "/device/poll",origin=PANEL_ORIGIN,extra=options)[0],403)

        device_request = {"manifest":MANIFEST,"results":[]}
        self.assertEqual(self.request("POST",api + "/device/poll",bearer=OWNER,body=device_request)[0],401)
        self.assertEqual(self.request("POST",api + "/device/poll",bearer=DEVICE,
                                      origin=PANEL_ORIGIN,body=device_request)[0],401)
        status, headers, _ = self.request("POST",api + "/device/poll",bearer=DEVICE,body=device_request)
        self.assertEqual(status,200)
        self.assertNotIn("Access-Control-Allow-Origin",headers)
        status, _, devices = self.request("GET",api + "/devices",bearer=OWNER,origin=PANEL_ORIGIN)
        self.assertEqual(status,200)
        self.assertEqual(devices,{"devices":[{"manifest":MANIFEST,"online":True}]})
        status, _, result = self.request("POST",api + "/commands",bearer=OWNER,origin=PANEL_ORIGIN,body={
            "deviceId":DEVICE_ID,"capability":"power","value":True
        })
        self.assertEqual(status,202)
        command_id = result["id"]
        _, _, pending = self.request("POST",api + "/device/poll",bearer=DEVICE,body=device_request)
        self.assertEqual(pending["command"]["id"],command_id)
        self.assertEqual(self.request("GET",api + "/commands/" + command_id,bearer=OWNER)[2],{"status":"pending"})
        device_request["results"] = [{"id":command_id,"ok":True}]
        self.request("POST",api + "/device/poll",bearer=DEVICE,body=device_request)
        self.assertEqual(self.request("GET",api + "/commands/" + command_id,bearer=OWNER)[2],{"status":"applied"})

    def test_ap_ticket_limits_expiry_and_signing_key(self):
        config = Config.from_env({**config_env(),"KONTUR_SETUP_SHA256":sha256(SETUP.encode()).hexdigest()})
        ticket = issue_enrollment_ticket(config,"a"*32,"b"*32,"x"*128,1_800_000_000)
        payload, signature = ticket.split(".")
        self.assertLessEqual(len(payload),320)
        self.assertEqual(len(signature),43)
        claims = verify_enrollment_ticket(config,ticket,1_800_000_899)
        self.assertEqual(claims.room,"x"*128)
        with self.assertRaisesRegex(RelayError,"enrollment-expired"):
            verify_enrollment_ticket(config,ticket,1_800_000_900)
        wrong_signature = b64url(hmac_digest(config.setup_hash,payload.encode("ascii"),"sha256"))
        with self.assertRaisesRegex(RelayError,"invalid-enrollment"):
            verify_enrollment_ticket(config,payload + "." + wrong_signature,1_800_000_100)

    def test_ap_enrollment_requires_owner_board_proof_and_replays_without_rebinding(self):
        with TemporaryDirectory() as directory:
            server = SimpleNamespace(
                config=Config.from_env({**config_env(),"KONTUR_SETUP_SHA256":sha256(SETUP.encode()).hexdigest()}),
                relay=self.relay, accounts=Accounts(Path(directory)/"homes.sqlite3"),
                authentication_slots=BoundedSemaphore(2)
            )

            def post(route, body, bearer=None):
                status, _, data = self.request(
                    "POST","/api/" + route,bearer=bearer,origin=PANEL_ORIGIN,body=body,server=server
                )
                return status,data

            def register(email, name):
                code,data = post("account/register",{"email":email,"name":name,"password":"password for test"})
                self.assertEqual(code,201)
                return data["sessionToken"]

            owner = register("owner@example.test","Owner")
            guest = register("guest@example.test","Guest")
            code,home = post("home/create",{"name":"Home","index":"test-home","state":{
                "rooms":[{"id":"living","name":"Living"},{"id":"study","name":"Study"}],"devices":[]
            }},owner)
            self.assertEqual(code,200)
            home_id = home["id"]
            _,invite = post("home/invite",{"homeId":home_id},owner)
            self.assertEqual(post("home/join",invite,guest)[0],200)
            start = "home/cloud/enroll/start"
            finish = "home/cloud/enroll/finish"
            request = {"homeId":home_id,"roomId":"living"}
            self.assertEqual(post(start,request)[0],401)
            self.assertEqual(post(start,request,guest)[0],403)
            self.assertEqual(post(start,{**request,"roomId":"missing"},owner)[0],400)
            status, result = post(start,request,owner)
            self.assertEqual(status,200)
            ticket = result["ticket"]
            message = ("kontur:ap:" + ticket + ":" + DEVICE_ID).encode("ascii")
            proof = hmac_digest(server.config.setup_hash,message,"sha256").hex()
            claim = {"ticket":ticket,"deviceId":DEVICE_ID,"proof":proof}
            self.assertEqual(post(finish,claim,guest)[0],403)
            self.assertEqual(post(finish,{**claim,"proof":"0"*64},owner),(400,{"error":"invalid-enrollment"}))
            self.assertEqual(post(finish,claim,owner),(409,{"error":"device-offline"}))
            self.poll()
            status, claimed = post(finish,claim,owner)
            self.assertEqual(status,200)
            self.assertEqual(claimed["state"]["devices"][0]["roomId"],"living")

            claimed["state"]["devices"][0]["roomId"] = "study"
            status, moved = post("home/save",{
                "homeId":home_id,"revision":claimed["revision"],"state":claimed["state"]
            },owner)
            self.assertEqual(status,200)
            self.now = 20
            status, replay = post(finish,claim,owner)
            self.assertEqual(status,200)
            self.assertEqual(replay["revision"],moved["revision"])
            self.assertEqual(replay["state"]["devices"][0]["roomId"],"study")
            self.assertFalse(replay["state"]["devices"][0]["online"])
            replay["state"]["devices"] = []
            self.assertEqual(post("home/save",{
                "homeId":home_id,"revision":replay["revision"],"state":replay["state"]
            },owner)[0],200)
            self.assertEqual(post(finish,claim,owner),(409,{"error":"invalid-enrollment"}))


if __name__ == "__main__":
    unittest.main()
