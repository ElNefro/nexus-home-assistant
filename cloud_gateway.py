"""Forward Kontur relay commands to an explicitly paired ESP32 on a private LAN."""

import argparse
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import ipaddress
import json
import logging
from pathlib import Path
import re
import ssl
import time

from cloud_server import (
    COMMAND_ID, COMMAND_TTL, DEVICE_ID, MAX_COMMANDS, ONLINE_WINDOW,
    RelayError, normalize_manifest,
)


CLOUD_HOST = "nexus-home-assistant-api.onrender.com"
CLOUD_POLL = "/api/cloud/device/poll"
LAN_PREFIX = "/kontur/v1/"
PRIVATE_NETWORKS = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}", re.ASCII)
MAX_JSON = 8192
POLL_INTERVAL = 2
LAN_TIMEOUT = 1.5
CLOUD_TIMEOUT = 8
MAX_DISPATCH_AGE = 4
RECEIPT_TIMEOUT = 8
RECEIPT_INTERVAL = 0.25
LOGGER = logging.getLogger("kontur-gateway")


class GatewayError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Config:
    device_id: str
    lan_address: str
    lan_device_token: str = field(repr=False)
    cloud_token: str = field(repr=False)

    @classmethod
    def load(cls, path):
        try:
            with path.open("rb") as source:
                raw = source.read(4097)
            settings = json.loads(raw.decode("utf-8-sig")) if len(raw) <= 4096 else None
        except (OSError, ValueError, RecursionError):
            raise GatewayError("Cannot read gateway configuration") from None
        names = ("device_id", "lan_address", "lan_device_token", "cloud_token")
        if type(settings) is not dict or set(settings) != set(names) or any(type(settings[name]) is not str for name in names):
            raise GatewayError("Configuration needs device_id, lan_address, lan_device_token and cloud_token")
        config = cls(**settings)
        try:
            address = ipaddress.IPv4Address(config.lan_address)
        except ValueError:
            raise GatewayError("lan_address must be an RFC1918 IPv4 address") from None
        if not any(address in network for network in PRIVATE_NETWORKS):
            raise GatewayError("lan_address must be an RFC1918 IPv4 address")
        if not DEVICE_ID.fullmatch(config.device_id):
            raise GatewayError("Invalid device_id")
        if not TOKEN.fullmatch(config.lan_device_token) or not TOKEN.fullmatch(config.cloud_token):
            raise GatewayError("Invalid LAN or Render device token")
        if config.lan_device_token == config.cloud_token:
            raise GatewayError("LAN and Render device tokens must be different")
        return config


def request_json(connection, remote, path, token=None, body=None):
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    try:
        connection.request("GET" if body is None else "POST", path, body=body, headers=headers)
        with connection.getresponse() as response:
            if not 200 <= response.status < 300:
                raise GatewayError(f"{remote} returned HTTP {response.status}", response.status)
            if response.headers.get_content_type() != "application/json":
                raise GatewayError(f"{remote} did not return JSON")
            content = response.read(MAX_JSON + 1)
        try:
            result = json.loads(content) if len(content) <= MAX_JSON else None
        except (ValueError, RecursionError):
            result = None
        if type(result) is not dict:
            raise GatewayError(f"{remote} returned an invalid JSON object")
        return result
    except (OSError, HTTPException) as error:
        raise GatewayError(f"{remote} connection failed ({type(error).__name__})") from None
    finally:
        connection.close()


class Gateway:
    def __init__(self, config):
        self.config = config
        self.tls = ssl.create_default_context()
        self.result = None
        self.last_poll = None
        self.recent = {}

    def lan(self, path, body=None, authenticated=True):
        connection = HTTPConnection(self.config.lan_address, 80, timeout=LAN_TIMEOUT)
        token = self.config.lan_device_token if authenticated else None
        return request_json(connection, "ESP32", LAN_PREFIX + path, token, body)

    def cloud(self, manifest):
        connection = HTTPSConnection(CLOUD_HOST, 443, timeout=CLOUD_TIMEOUT, context=self.tls)
        payload = {"manifest": manifest, "results": [self.result] if self.result else []}
        response = request_json(connection, "Render", CLOUD_POLL, self.config.cloud_token, payload)
        if "command" not in response:
            raise GatewayError("Render returned no command field")
        command = response["command"]
        if command is not None:
            command_id = command.get("id") if type(command) is dict else None
            if (type(command_id) is not str or not COMMAND_ID.fullmatch(command_id)
                    or command.get("capability") != "power" or command.get("value") is not True):
                raise GatewayError("Render returned an invalid command")
            return {"id": command_id, "capability": "power", "value": True}
        return None

    def manifest(self):
        raw = self.lan("manifest", authenticated=False)
        try:
            return normalize_manifest(raw, self.config.device_id)
        except RelayError:
            raise GatewayError("The ESP32 manifest does not match device_id and the indicator capability") from None

    def receipt(self, command_id):
        try:
            response = self.lan("cloud/commands/" + command_id)
        except GatewayError as error:
            if error.status == 404:
                return None
            raise
        return self.receipt_status(response)

    @staticmethod
    def receipt_status(response):
        status = response.get("status")
        if status not in ("pending", "applied", "failed"):
            raise GatewayError("ESP32 returned an invalid command receipt")
        return status

    def apply(self, command, dispatch_until):
        command_id = command["id"]
        status = self.receipt(command_id)
        if status is None and time.monotonic() < dispatch_until:
            try:
                status = self.receipt_status(self.lan("cloud/command", command))
            except GatewayError:
                # The device may already have received the command. Query the same
                # receipt instead of writing again, even if the board has rebooted.
                status = "pending"
        if status == "pending":
            deadline = time.monotonic() + RECEIPT_TIMEOUT
            while time.monotonic() < deadline:
                time.sleep(RECEIPT_INTERVAL)
                try:
                    status = self.receipt(command_id)
                except GatewayError:
                    continue
                if status in ("applied", "failed"):
                    break
        return status == "applied"

    def step(self):
        manifest = self.manifest()
        state = self.lan("state")
        if type(state.get("power")) is not bool:
            raise GatewayError("ESP32 did not confirm its state")
        started = time.monotonic()
        fresh_session = self.last_poll is not None and started - self.last_poll < ONLINE_WINDOW
        command = self.cloud(manifest)
        self.last_poll = time.monotonic()
        self.result = None
        if command is not None:
            command_id = command["id"]
            if command_id in self.recent:
                applied = self.recent[command_id]
            else:
                dispatch_until = started + min(MAX_DISPATCH_AGE, COMMAND_TTL) if fresh_session else 0
                try:
                    applied = self.apply(command, dispatch_until)
                except GatewayError as error:
                    LOGGER.warning("%s", error)
                    applied = False
                self.recent[command_id] = applied
                if len(self.recent) > MAX_COMMANDS:
                    del self.recent[next(iter(self.recent))]
            self.result = {"id": command_id, "ok": applied}
            LOGGER.info("ESP32 command %s", "applied" if applied else "unconfirmed")

    def run(self):
        outage = None
        while True:
            try:
                was_online = self.last_poll is not None
                self.step()
                if not was_online:
                    LOGGER.info("ESP32 connected to Render through the PC")
                outage = None
            except GatewayError as error:
                self.last_poll = None
                if str(error) != outage:
                    LOGGER.warning("%s", error)
                    outage = str(error)
            time.sleep(POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Path to a private gateway JSON file")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        Gateway(Config.load(arguments.config)).run()
    except GatewayError as error:
        parser.exit(2, f"{error}\n")
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
