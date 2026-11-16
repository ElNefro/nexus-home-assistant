"""Kontur Wi-Fi light for the MicroPython ESP32 port."""

import json
import socket
from machine import Pin, unique_id
from network import WLAN
from time import sleep_ms, ticks_add, ticks_diff, ticks_ms

import kontur_config


HTTP_PORT = 80
DISCOVERY_PORT = 42424
DISCOVERY_QUERY = b"KONTUR-DISCOVER-1 "
DISCOVERY_RESPONSE = b"KONTUR-HERE-1 "
NONCE_LENGTH = 24
MAX_HEADERS = 1024
MAX_COMMAND = 256
REQUEST_TIMEOUT_MS = 850
RETRY_MS = 5000
BLINK_ON_MS = 140
BLINK_OFF_MS = 140
BETWEEN_BURSTS_MS = 320
MAX_PENDING_BURSTS = 8
GATEWAY_RECEIPT_LIMIT = 96
GATEWAY_REPLAY_MS = 240000
DEVICE_NAME = "Светильник ESP32"
ROUTE_PREFIX = b"/kontur/v1/"
GATEWAY_COMMAND = ROUTE_PREFIX + b"cloud/command"
GATEWAY_STATUS_PREFIX = ROUTE_PREFIX + b"cloud/commands/"
TOKEN_CHARACTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"

STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    408: "Request Timeout",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    503: "Service Unavailable",
}


class RequestError(Exception):
    pass


class Indicator:
    def __init__(self, pin, active_low):
        self.idle_level = int(active_low)
        self.pin = Pin(pin, Pin.OUT, value=self.idle_level)
        self.pending = []
        self.on_done = None
        self.reset()

    def blink(self, count, on_done=None):
        if len(self.pending) >= MAX_PENDING_BURSTS:
            raise RequestError(503, "blink-queue-full")
        self.pending.append((count, on_done))
        self.poll(ticks_ms())

    def poll(self, now):
        if self.next_edge is not None and ticks_diff(now, self.next_edge) < 0:
            return
        if self.lit:
            self.pin.value(self.idle_level)
            self.lit = False
            self.remaining -= 1
            pause = BLINK_OFF_MS if self.remaining else BETWEEN_BURSTS_MS
            self.next_edge = ticks_add(now, pause)
            if not self.remaining and self.on_done is not None:
                done, self.on_done = self.on_done, None
                done(True)
            return
        if not self.remaining and self.pending:
            self.remaining, self.on_done = self.pending.pop(0)
        if self.remaining:
            self.pin.value(1 - self.idle_level)
            self.lit = True
            self.next_edge = ticks_add(now, BLINK_ON_MS)
        else:
            self.next_edge = None

    def reset(self):
        self.next_edge = None
        self.remaining = 0
        self.lit = False
        self.pin.value(self.idle_level)
        if self.on_done is not None:
            done, self.on_done = self.on_done, None
            done(False)
        for _, done in self.pending:
            if done is not None:
                done(False)
        self.pending.clear()


def valid_gateway_id(command_id):
    return type(command_id) is str and 1 <= len(command_id) <= 96 and all(
        character in TOKEN_CHARACTERS for character in command_id
    )


class GatewayCommands:
    def __init__(self, indicator):
        self.indicator = indicator
        self.receipts = {}

    def prune(self):
        now = ticks_ms()
        expired = [
            command_id for command_id, (_, done_at) in self.receipts.items()
            if done_at is not None and ticks_diff(now, done_at) >= GATEWAY_REPLAY_MS
        ]
        for command_id in expired:
            del self.receipts[command_id]

    def complete(self, command_id, ok):
        if self.receipts[command_id][0] == "pending":
            self.receipts[command_id] = ("applied" if ok else "failed", ticks_ms())

    def submit(self, command):
        command_id = command.get("id")
        if not valid_gateway_id(command_id) or command.get("capability") != "power" or command.get("value") is not True:
            raise RequestError(400, "invalid-gateway-command")
        self.prune()
        if command_id in self.receipts:
            return {"status": self.receipts[command_id][0]}
        if len(self.receipts) >= GATEWAY_RECEIPT_LIMIT:
            raise RequestError(503, "gateway-history-full")
        self.receipts[command_id] = ("pending", None)
        try:
            self.indicator.blink(4, lambda ok: self.complete(command_id, ok))
            print("LED pattern: cloud gateway command, 4 blinks")
        except (RequestError, OSError, ValueError):
            self.complete(command_id, False)
        return {"status": self.receipts[command_id][0]}

    def status(self, raw_id):
        if not 1 <= len(raw_id) <= 96 or any(byte > 127 for byte in raw_id):
            raise RequestError(404, "gateway-command-not-found")
        command_id = raw_id.decode()
        self.prune()
        if not valid_gateway_id(command_id) or command_id not in self.receipts:
            raise RequestError(404, "gateway-command-not-found")
        return {"status": self.receipts[command_id][0]}


def read_chunk(client, size, deadline):
    remaining = ticks_diff(deadline, ticks_ms())
    if remaining <= 0:
        raise RequestError(408, "request-timeout")
    client.settimeout(remaining / 1000)
    return client.recv(size)


def read_headers(client, deadline):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = read_chunk(client, min(256, MAX_HEADERS + 4 - len(data)), deadline)
        if not chunk:
            raise RequestError(400, "invalid-request")
        data += chunk
        if len(data) >= MAX_HEADERS + 4 and b"\r\n\r\n" not in data:
            raise RequestError(413, "headers-too-large")
    header, _, body = data.partition(b"\r\n\r\n")
    lines = header.split(b"\r\n")
    request = lines[0].split(b" ")
    if len(request) != 3 or request[2] not in (b"HTTP/1.0", b"HTTP/1.1"):
        raise RequestError(400, "invalid-request")
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        name = name.lower()
        if not separator or not name or name in headers:
            raise RequestError(400, "invalid-request")
        headers[name] = value.strip()
    return request[0], request[1], headers, body


def authorized(header, expected):
    if header is None or len(header) != len(expected):
        return False
    difference = 0
    for index in range(len(expected)):
        difference |= header[index] ^ expected[index]
    return difference == 0


def read_command(client, headers, body, deadline, fields=("capability", "value")):
    content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
    if content_type != b"application/json":
        raise RequestError(415, "json-required")
    length_text = headers.get(b"content-length", b"")
    if b"transfer-encoding" in headers or not length_text or not length_text.isdigit():
        raise RequestError(400, "invalid-command-body")
    if len(length_text) > 3:
        raise RequestError(413, "command-too-large")
    length = int(length_text)
    if length > MAX_COMMAND:
        raise RequestError(413, "command-too-large")
    if length == 0 or len(body) > length:
        raise RequestError(400, "invalid-command-body")
    while len(body) < length:
        chunk = read_chunk(client, length - len(body), deadline)
        if not chunk:
            raise RequestError(400, "invalid-command-body")
        body += chunk
    try:
        command = json.loads(body)
    except ValueError:
        raise RequestError(400, "invalid-command")
    if type(command) is not dict or len(command) != len(fields) or any(field not in command for field in fields):
        raise RequestError(400, "invalid-command")
    return command


def active_enrollment(enrollment):
    if enrollment and ticks_diff(enrollment[1], ticks_ms()) > 0:
        return enrollment[0]
    return None


def route(client, manifest, expected_token, indicator, gateway=None, enrollment=None):
    deadline = ticks_add(ticks_ms(), REQUEST_TIMEOUT_MS)
    method, path, headers, body = read_headers(client, deadline)
    if method == b"GET" and path == ROUTE_PREFIX + b"manifest":
        return manifest
    read = method == b"GET" and path == ROUTE_PREFIX + b"state"
    write = method == b"POST" and path == ROUTE_PREFIX + b"command"
    pair = method == b"POST" and path == ROUTE_PREFIX + b"pair"
    gateway_submit = gateway is not None and method == b"POST" and path == GATEWAY_COMMAND
    gateway_status = gateway is not None and method == b"GET" and path.startswith(GATEWAY_STATUS_PREFIX)
    if not (read or write or pair or gateway_submit or gateway_status):
        raise RequestError(404, "not-found")
    if not authorized(headers.get(b"authorization"), expected_token):
        raise RequestError(401, "token-required")
    if read and gateway is not None:
        nonce = active_enrollment(enrollment)
        if nonce:
            return {"power": False, "enrollmentNonce": nonce}
    if gateway_submit:
        command = read_command(client, headers, body, deadline, ("id", "capability", "value"))
        return gateway.submit(command)
    if gateway_status:
        return gateway.status(path[len(GATEWAY_STATUS_PREFIX):])
    if pair:
        if body or b"transfer-encoding" in headers or headers.get(b"content-length", b"0") != b"0":
            raise RequestError(400, "expected-empty-body")
        indicator.blink(1)
        print("LED pattern: pairing, 1 blink")
    if write:
        command = read_command(client, headers, body, deadline)
        value = command["value"]
        if command.get("capability") != "power" or type(value) is not bool:
            raise RequestError(400, "invalid-capability-or-value")
        if value:
            indicator.blink(4)
            print("LED pattern: command, 4 blinks")
            return {"power": True}
    return {"power": False}


def serve_client(client, manifest, expected_token, indicator, gateway=None, enrollment=None):
    try:
        try:
            result = route(client, manifest, expected_token, indicator, gateway, enrollment)
            status = 200
        except RequestError as error:
            status, message = error.args
            result = {"error": message}
        payload = json.dumps(result).encode("utf-8")
        response = (
            "HTTP/1.1 %d %s\r\nContent-Type: application/json; charset=utf-8\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\nContent-Length: %d\r\n\r\n"
        ) % (status, STATUS_TEXT[status], len(payload))
        client.settimeout(0.3)
        client.write(response.encode("ascii") + payload)
    except OSError:
        pass
    finally:
        client.close()


def http_listener(ip):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((ip, HTTP_PORT))
        listener.listen(4)
        listener.setblocking(False)
        return listener
    except OSError:
        listener.close()
        raise


def discovery_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        listener.bind(("0.0.0.0", DISCOVERY_PORT))
        listener.setblocking(False)
        return listener
    except OSError:
        listener.close()
        raise


def poll_discovery(listener):
    # Read at most one datagram per iteration so discovery cannot starve HTTP.
    try:
        # Leave room for one extra byte so an overlong, truncated query fails validation.
        request, sender = listener.recvfrom(len(DISCOVERY_QUERY) + NONCE_LENGTH + 1)
    except OSError:
        return
    if len(request) != len(DISCOVERY_QUERY) + NONCE_LENGTH or not request.startswith(DISCOVERY_QUERY):
        return
    nonce = request[len(DISCOVERY_QUERY):]
    if not all(48 <= digit <= 57 or 97 <= digit <= 102 for digit in nonce):
        return
    try:
        listener.sendto(DISCOVERY_RESPONSE + nonce, sender)
    except OSError:
        pass


def manifest_for(device_id):
    return {
        "schemaVersion": 1,
        "id": device_id,
        "kind": "light",
        "name": DEVICE_NAME,
        "requiresToken": True,
        "pairSignal": True,
        "capabilities": [
            {"id": "power", "type": "switch", "label": "Мигнуть", "momentary": True},
        ],
    }


def start_cloud(manifest, enrollment=None):
    url = getattr(kontur_config, "CLOUD_URL", "")
    token = getattr(kontur_config, "CLOUD_TOKEN", "")
    if not url and not token:
        return None
    try:
        from kontur_cloud import CloudClient
        ca_file = getattr(kontur_config, "CLOUD_CA_FILE", "/kontur-cloud-ca.pem")
        return CloudClient(url, token, ca_file, manifest, lambda: active_enrollment(enrollment))
    except (ImportError, ValueError, OSError, MemoryError) as error:
        print("Cloud configuration unavailable:", type(error).__name__)
        return None


def handle_cloud_command(cloud, indicator):
    command = cloud.take_command()
    if command is None:
        return
    command_id, capability, value = command
    if capability != "power" or value is not True:
        cloud.complete(command_id, False)
        return
    try:
        indicator.blink(4, lambda ok: cloud.complete(command_id, ok))
        print("LED pattern: cloud command, 4 blinks")
    except (RequestError, OSError, ValueError):
        cloud.complete(command_id, False)


def run():
    led_gpio = kontur_config.LED_GPIO
    led_active_low = getattr(kontur_config, "LED_ACTIVE_LOW", False)
    if type(led_gpio) is not int or led_gpio < 0 or type(led_active_low) is not bool:
        print("Set LED_GPIO and LED_ACTIVE_LOW for your onboard ESP32 LED")
        return
    device_token = kontur_config.DEVICE_TOKEN
    if type(device_token) is not str or not 32 <= len(device_token) <= 128 or any(
            character not in TOKEN_CHARACTERS for character in device_token):
        print("Configure a 32-128 character device token in kontur_config.py")
        return
    indicator = Indicator(led_gpio, led_active_low)
    # The original Arduino sketch formats its eFuse MAC as a little-endian integer.
    device_id = "light-" + "".join("%02x" % part for part in reversed(unique_id()))
    manifest = manifest_for(device_id)
    expected_token = b"Bearer " + device_token.encode("ascii")
    wlan = WLAN(getattr(WLAN, "IF_STA", 0))
    listener = None
    discovery = None
    active_ip = None
    cloud = None
    gateway = None
    provisioning = None
    enrollment = None
    if (getattr(kontur_config, "CLOUD_GATEWAY", False) is True
            and not getattr(kontur_config, "CLOUD_URL", "") and not getattr(kontur_config, "CLOUD_TOKEN", "")):
        gateway = GatewayCommands(indicator)
        print("Cloud gateway mode enabled")
    try:
        setup_key = getattr(kontur_config, "SETUP_KEY", "")
        if setup_key:
            from kontur_provision import Provisioner, load_wifi
            credentials = load_wifi()
            if credentials is None:
                wlan.active(False)
                ap_password = getattr(kontur_config, "SETUP_AP_PASSWORD", "")
                if ap_password:
                    from kontur_portal import Portal, PortalExpired
                    portal = Portal(device_id, DEVICE_NAME, setup_key, ap_password)
                    print("Wi-Fi setup hotspot:", portal.ssid, "at http://192.168.4.1/")
                    try:
                        while not portal.poll(wlan):
                            sleep_ms(20)
                    except PortalExpired:
                        print("Wi-Fi setup hotspot expired. Power-cycle the board to retry.")
                        return
                    finally:
                        portal.close()
                    enrollment = portal.enrollment
                    portal = None
                else:
                    provisioning = Provisioner(device_id, DEVICE_NAME, setup_key)
                    print("Wi-Fi setup available over Bluetooth")
                    while not provisioning.poll(wlan):
                        sleep_ms(20)
                print("Wi-Fi setup completed")
            else:
                wlan.active(True)
                wlan.connect(*credentials)
            credentials = None
        else:
            ssid = getattr(kontur_config, "WIFI_SSID", "")
            if not ssid:
                print("Configure a setup key in kontur_config.py")
                return
            wlan.active(True)
            wlan.connect(ssid, getattr(kontur_config, "WIFI_PASSWORD", ""))
        cloud = start_cloud(manifest, enrollment)
        print("Starting Wi-Fi services for %s" % device_id)
        retry_at = ticks_ms()
        waiting_at = ticks_add(retry_at, 15000)
        while True:
            now = ticks_ms()
            if provisioning is not None and provisioning.finished(now):
                provisioning.close()
                provisioning = None
            ip = wlan.ifconfig()[0] if wlan.isconnected() else None
            if ip != active_ip:
                if cloud is not None:
                    cloud.set_connected(ip is not None)
                if discovery is not None:
                    discovery.close()
                    discovery = None
                if listener is not None:
                    listener.close()
                    listener = None
                if active_ip is not None:
                    indicator.reset()
                    print("Wi-Fi connection changed")
                active_ip = ip
                retry_at = now
            if ip is None and ticks_diff(now, waiting_at) >= 0:
                print("Waiting for Wi-Fi, status:", wlan.status())
                waiting_at = ticks_add(now, 15000)
            if ip and ticks_diff(now, retry_at) >= 0:
                retry_at = ticks_add(now, RETRY_MS)
                if listener is None:
                    try:
                        listener = http_listener(ip)
                        print("HTTP address: http://" + ip)
                    except OSError as error:
                        print("HTTP setup failed:", error)
                if listener is not None and discovery is None:
                    try:
                        discovery = discovery_listener()
                        print("Announcing %s via Kontur UDP on %d" % (device_id, DISCOVERY_PORT))
                    except OSError as error:
                        print("UDP discovery setup failed:", error)
            if discovery is not None:
                poll_discovery(discovery)
            indicator.poll(now)
            if cloud is not None:
                handle_cloud_command(cloud, indicator)
            if listener is not None:
                try:
                    client, _ = listener.accept()
                except OSError:
                    pass
                else:
                    serve_client(client, manifest, expected_token, indicator, gateway, enrollment)
            sleep_ms(10)
    finally:
        indicator.reset()
        if provisioning is not None:
            provisioning.close()
        if cloud is not None:
            cloud.close()
        if discovery is not None:
            discovery.close()
        if listener is not None:
            listener.close()


if __name__ == "__main__":
    run()
