import tempfile
import unittest
from pathlib import Path

from server_accounts import AccountError, Accounts, clean_home
from server_automation import decision


def sample():
    return {
        "rooms": [{"id": "living", "name": "Гостиная"}],
        "devices": [{
            "id": "lamp", "kind": "light", "name": "Светильник",
            "roomId": "living", "online": True, "state": {"power": False},
            "capabilities": [{"id": "power", "label": "Питание", "type": "switch"}]
        }],
        "roomId": "living",
        "profile": {"name": "Impersonator", "role": "Admin"}
    }


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "home.db"
        self.accounts = Accounts(self.file)

    def request(self, route, body=None, cookie=None):
        return self.accounts.route("GET" if body is None else "POST", route, body, cookie)

    def register(self, email, name):
        status, payload, headers = self.request("/api/account/register", {
            "email": email, "password": "a local test password", "name": name
        })
        self.assertEqual(status, 201)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return payload, headers["Set-Cookie"].split(";", 1)[0]

    def test_owner_invites_member_and_revocation_stops_shared_commands(self):
        _, owner = self.register("owner@example.test", "Owner")
        member, guest = self.register("member@example.test", "Member")
        status, home, _ = self.request("/api/home/create", {"name":"Home", "state":sample()}, owner)
        self.assertEqual(status, 200)
        self.assertEqual(home["state"]["profile"]["name"], "Owner")
        home_id = home["id"]
        self.assertEqual(self.request("/api/home/get", {"homeId":home_id}, guest)[0], 403)
        _, invite, _ = self.request("/api/home/invite", {"homeId":home_id}, owner)
        status, joined, _ = self.request("/api/home/join", invite, guest)
        self.assertEqual(status, 200)
        self.assertEqual(joined["role"], "member")
        self.assertEqual(self.request("/api/home/join", invite, guest)[0], 404)
        command = {"homeId":home_id,"id":"lamp","capability":"power","value":True}
        status, result, _ = self.request("/api/home/command", command, guest)
        self.assertEqual(status, 200)
        self.assertTrue(result["state"]["devices"][0]["state"]["power"])
        self.assertEqual(self.request("/api/home/save", {
            "homeId":home_id, "state":sample(), "revision":result["revision"]
        }, guest)[0], 403)
        self.assertEqual(self.request("/api/home/revoke", {
            "homeId":home_id, "userId":member["user"]["id"]
        }, owner)[0], 200)
        self.assertEqual(self.request("/api/home/command", command, guest)[0], 403)
        with self.assertRaises(AccountError):
            self.accounts.authorize(guest,home_id)

    def test_cas_and_restart_keep_state(self):
        _, owner = self.register("owner@example.test", "Owner")
        _, home, _ = self.request("/api/home/create", {"name":"Home","state":sample()}, owner)
        body = {"homeId":home["id"],"state":sample(),"revision":home["revision"]}
        body["state"]["rooms"][0]["name"] = "Office"
        self.assertEqual(self.request("/api/home/save", body, owner)[0], 200)
        self.assertEqual(self.request("/api/home/save", body, owner)[0], 409)
        self.accounts = Accounts(self.file)
        status, returned, _ = self.request("/api/home/get", {"homeId":home["id"]}, owner)
        self.assertEqual(status, 200)
        self.assertEqual(returned["state"]["rooms"][0]["name"], "Office")

    def test_recovery_rotates_code_and_ends_old_session(self):
        registered, old_cookie = self.register("owner@example.test", "Owner")
        body = {"email":"owner@example.test","recoveryCode":registered["recoveryCode"],
                "password":"a replacement password"}
        status, recovered, headers = self.request("/api/account/recover", body)
        self.assertEqual(status, 200)
        self.assertNotEqual(recovered["recoveryCode"], body["recoveryCode"])
        self.assertEqual(self.request("/api/account/recover", body)[0], 401)
        self.assertIsNone(self.request("/api/account/session", cookie=old_cookie)[1]["user"])
        cookie = headers["Set-Cookie"].split(";",1)[0]
        self.assertEqual(self.request("/api/account/session", cookie=cookie)[1]["user"]["name"], "Owner")

    def test_automation_rejects_stale_revision(self):
        _, owner = self.register("owner@example.test", "Owner")
        _, home, _ = self.request("/api/home/create", {"name":"Home","state":sample()}, owner)
        body = {"homeId":home["id"],"id":"lamp","schedule":{"mode":"interval","period":30}}
        _, current, _ = self.request("/api/home/schedule",body,owner)
        self.assertFalse(self.accounts.apply_automation(home["id"],"lamp",home["revision"],True,500))
        self.assertTrue(self.accounts.apply_automation(home["id"],"lamp",current["revision"],True,500))
        state = self.accounts.iter_homes()[0]["state"]["devices"][0]
        self.assertTrue(state["state"]["power"])
        self.assertEqual(state["schedule"]["nextRun"], 500)

    def test_lan_binding_is_exclusive_and_preserves_confirmed_state(self):
        _, owner = self.register("owner@example.test", "Owner")
        _, another = self.register("other@example.test", "Other")
        _, home, _ = self.request("/api/home/create", {"name":"Home"}, owner)
        _, second, _ = self.request("/api/home/create", {"name":"Second"}, another)
        manifest = {
            "id":"test-lamp", "name":"Desk lamp", "kind":"light",
            "capabilities":[{"id":"power","type":"switch","label":"Power"}]
        }
        device = self.accounts.bind_device(home["id"],manifest,"living",{"power":False})
        with self.assertRaises(AccountError) as failure:
            self.accounts.bind_device(second["id"],manifest,"living",{"power":False})
        self.assertEqual(failure.exception.code,"device-owned")
        self.assertTrue(self.accounts.has_device(home["id"],device["id"]))
        _, current, _ = self.request("/api/home/get",{"homeId":home["id"]},owner)
        settings = current["state"]["devices"][0]
        settings["state"]["power"] = True
        settings["commandFeedback"] = "transmitted"
        settings["description"] = "Unconfirmed description"
        del settings["transport"]
        status, saved, _ = self.request("/api/home/save",{
            "homeId":home["id"],"state":current["state"],"revision":current["revision"]
        },owner)
        self.assertEqual(status,200)
        self.assertEqual(saved["state"]["devices"][0]["transport"],"lan")
        self.assertFalse(saved["state"]["devices"][0]["state"]["power"])
        self.assertNotIn("commandFeedback", saved["state"]["devices"][0])
        self.assertNotIn("description", saved["state"]["devices"][0])
        self.accounts.update_device_state(home["id"],device["id"],{"power":True},True)
        self.accounts.mark_lan_offline()
        _, after_reboot, _ = self.request("/api/home/get",{"homeId":home["id"]},owner)
        self.assertFalse(after_reboot["state"]["devices"][0]["online"])
        self.assertTrue(after_reboot["state"]["devices"][0]["state"]["power"])
        after_reboot["state"]["devices"] = []
        self.assertEqual(self.request("/api/home/save",{
            "homeId":home["id"],"state":after_reboot["state"],"revision":after_reboot["revision"]
        },owner)[0],200)
        self.assertFalse(self.accounts.has_device(home["id"],device["id"]))
        self.assertTrue(self.accounts.bind_device(second["id"],manifest,"living",{"power":False})["online"])

    def test_ir_binding_keeps_validated_metadata_and_discards_previous_power_schedule(self):
        _, owner = self.register("owner@example.test", "Owner")
        _, home, _ = self.request("/api/home/create", {"name": "Home"}, owner)
        manifest = {"id": "ir-projector-0123456789ab", "name": "BL-XK01", "kind": "light",
                    "capabilities": [{"id": "power", "type": "switch", "label": "Power"}]}
        old = self.accounts.bind_device(home["id"], manifest, "living", {"power": True})
        self.assertEqual(self.request("/api/home/schedule", {
            "homeId": home["id"], "id": old["id"], "schedule": {"mode": "interval", "period": 15}
        }, owner)[0], 200)

        actions = ("toggle", "brightness", "white", "red", "green", "blue", "purple", "rotation")
        manifest.update(commandFeedback="transmitted", description="ИК-пульт без обратной связи.", capabilities=[
            {"id": action, "type": "switch", "label": action, "momentary": True,
             **({"description": "Сначала включите синий и красный свет."} if action == "purple" else {})}
            for action in actions
        ])
        idle = dict.fromkeys(actions, False)
        device = self.accounts.bind_device(home["id"], manifest, "living", dict.fromkeys(actions, True))
        self.assertEqual(device["state"], idle)
        self.assertNotIn("schedule", device)
        self.assertEqual(device["commandFeedback"], manifest["commandFeedback"])
        self.assertEqual(device["description"], manifest["description"])
        self.assertEqual(device["capabilities"], manifest["capabilities"])
        self.assertEqual(self.request("/api/home/schedule", {
            "homeId": home["id"], "id": device["id"], "schedule": {"mode": "interval", "period": 15}
        }, owner)[0], 400)

        self.accounts.update_device_state(home["id"], device["id"], {"purple": True}, True)
        _, snapshot, _ = self.request("/api/home/get", {"homeId": home["id"]}, owner)
        proposal = snapshot["state"]["devices"][0]
        self.assertEqual(proposal["state"], idle)
        proposal["description"] = "Forged description"
        proposal.pop("commandFeedback")
        proposal["capabilities"] = [{"id": "power", "type": "switch", "label": "Forged power"}]
        proposal["state"] = {"power": True}
        proposal["schedule"] = {"mode": "interval", "period": 15, "nextRun": 1}
        status, saved, _ = self.request("/api/home/save", {
            "homeId": home["id"], "state": snapshot["state"], "revision": snapshot["revision"]
        }, owner)
        self.assertEqual(status, 200)
        confirmed = saved["state"]["devices"][0]
        self.assertEqual(confirmed["description"], manifest["description"])
        self.assertEqual(confirmed["commandFeedback"], "transmitted")
        self.assertEqual(confirmed["capabilities"], manifest["capabilities"])
        self.assertEqual(confirmed["state"], idle)
        self.assertNotIn("schedule", confirmed)
        self.assertEqual(decision({**confirmed, "schedule": proposal["schedule"]}, [], 60_000, None), (None, None))
        self.assertFalse(self.accounts.apply_automation(home["id"], device["id"], saved["revision"], True, 120_000))
        self.accounts = Accounts(self.file)
        _, restarted, _ = self.request("/api/home/get", {"homeId": home["id"]}, owner)
        self.assertEqual(restarted["state"]["devices"], [confirmed])

    def test_known_device_metadata_requires_valid_values(self):
        for key, value in (("commandFeedback", "applied"), ("commandFeedback", None),
                           ("description", None), ("description", "x" * 501),
                           ("description", "unsafe\x00text"), ("description", "\ud800")):
            state = sample()
            state["devices"][0][key] = value
            with self.subTest(key=key, value=repr(value)), self.assertRaises(AccountError):
                clean_home(state)
        for key, value in (("description", None), ("description", "x" * 501),
                           ("description", "unsafe\x00text"), ("momentary", "true")):
            state = sample()
            state["devices"][0]["capabilities"][0][key] = value
            with self.subTest(capability_key=key, value=repr(value)), self.assertRaises(AccountError):
                clean_home(state)
        state = sample()
        state["devices"][0].update(commandFeedback="transmitted", description="x" * 500)
        state["devices"][0]["capabilities"][0]["description"] = "x" * 500
        self.assertEqual(clean_home(state)["devices"][0]["description"], "x" * 500)

    def test_home_index_is_case_insensitive_and_survives_restart(self):
        _, owner = self.register("owner@example.test","Owner")
        _, other = self.register("other@example.test","Other")
        status, home, _ = self.request("/api/home/create",{"name":"Home","index":"house-12"},owner)
        self.assertEqual(status,200)
        self.assertEqual(home["index"],"HOUSE-12")
        status, body, _ = self.request("/api/home/create",{"name":"Other","index":"House-12"},other)
        self.assertEqual((status,body["error"]),(409,"home-index-in-use"))
        self.assertEqual(self.request("/api/home/create",{"name":"Other","index":"house--12"},other)[0],400)
        self.accounts = Accounts(self.file)
        _, account, _ = self.request("/api/account/session",cookie=owner)
        self.assertEqual(account["homes"],[{
            "id":home["id"],"name":"Home","index":"HOUSE-12","role":"owner"
        }])

    def test_prefix_changes_display_without_granting_access(self):
        _, owner = self.register("owner@example.test","Owner")
        member, guest = self.register("guest@example.test","Guest")
        _, home, _ = self.request("/api/home/create",{"name":"Home","index":"house-12"},owner)
        _, invitation, _ = self.request("/api/home/invite",{"homeId":home["id"]},owner)
        _, shared, _ = self.request("/api/home/join",invitation,guest)
        self.assertEqual(shared["index"],"HOUSE-12")
        self.assertEqual(shared["prefix"],"Семья")
        self.assertEqual({row["role"]:row["prefix"] for row in shared["members"]},
                         {"owner":"Админ","member":"Семья"})
        payload = {"homeId":home["id"],"userId":member["user"]["id"],"prefix":"Админ"}
        self.assertEqual(self.request("/api/home/member-prefix",payload,guest)[0],403)
        status, changed, _ = self.request("/api/home/member-prefix",payload,owner)
        self.assertEqual(status,200)
        self.assertGreater(changed["revision"],shared["revision"])
        _, fresh_guest, _ = self.request("/api/home/get",{"homeId":home["id"]},guest)
        self.assertEqual(fresh_guest["prefix"],"Админ")
        self.assertEqual(fresh_guest["role"],"member")
        self.assertEqual(self.request("/api/home/invite",{"homeId":home["id"]},guest)[0],403)
        _, reset, _ = self.request("/api/home/member-prefix",{**payload,"prefix":""},owner)
        self.assertEqual(next(row for row in reset["members"] if row["role"] == "member")["prefix"],"Семья")

    def test_cloud_binding_reserves_same_board_across_transports(self):
        _, owner = self.register("owner@example.test","Owner")
        _, other = self.register("other@example.test","Other")
        _, first, _ = self.request("/api/home/create",{"name":"First"},owner)
        _, second, _ = self.request("/api/home/create",{"name":"Second"},other)
        manifest = {
            "id":"test-lamp","name":"Desk lamp","kind":"light",
            "capabilities":[{"id":"power","type":"switch","label":"Power","momentary":True}]
        }
        old_device = self.accounts.bind_device(first["id"],manifest,"living",{"power":False})
        with self.assertRaisesRegex(AccountError,"device-owned"):
            self.accounts.bind_cloud_device(second["id"],manifest,"living")
        device = self.accounts.bind_cloud_device(first["id"],manifest,"living")
        self.assertEqual(device["id"],"cloud-test-lamp")
        self.assertEqual(device["state"],{"power":False})
        self.assertIsNone(self.accounts.lan_owner(old_device["id"]))
        with self.assertRaisesRegex(AccountError,"device-owned"):
            self.accounts.bind_device(second["id"],manifest,"living",{"power":False})
        self.accounts = Accounts(self.file)
        self.assertEqual(self.accounts.cloud_owner(device["id"]),first["id"])
        _, snapshot, _ = self.request("/api/home/get",{"homeId":first["id"]},owner)
        self.assertEqual(snapshot["state"]["devices"],[device])
        snapshot["state"]["devices"] = []
        self.assertEqual(self.request("/api/home/save",{
            "homeId":first["id"],"state":snapshot["state"],"revision":snapshot["revision"]
        },owner)[0],200)
        self.assertIsNone(self.accounts.cloud_owner(device["id"]))


if __name__ == "__main__":
    unittest.main()
