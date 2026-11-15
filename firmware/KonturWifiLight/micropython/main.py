"""Kontur Wi-Fi light for the MicroPython ESP32 port."""

import json
import socket
from machine import PWM, Pin, unique_id
from network import WLAN
from time import sleep_ms, ticks_add, ticks_diff, ticks_ms

from kontur_config import DEVICE_TOKEN, LED_GPIO, WIFI_PASSWORD, WIFI_SSID


HTTP_PORT = 80
DISCOVERY_PORT = 42424
DISCOVERY_QUERY = b"KONTUR-DISCOVER-1 "
DISCOVERY_RESPONSE = b"KONTUR-HERE-1 "
NONCE_LENGTH = 24
MAX_HEADERS = 1024
MAX_COMMAND = 256
REQUEST_TIMEOUT_MS = 850
RETRY_MS = 5000
DEVICE_NAME = "Светильник ESP32"
ROUTE_PREFIX = b"/kontur/v1/"
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


class Light:
    def __init__(self, pin):
        self.pin = Pin(pin, Pin.OUT, value=0)
        self.output = PWM(self.pin, freq=5000, duty_u16=0)
        self.power = False
        self.brightness = 50

    def set(self, power, brightness):
        if self.output is None:
            raise RequestError(503, "led-output-unavailable")
        try:
            self.output.duty_u16((brightness * 65535 + 50) // 100 if power else 0)
        except (OSError, ValueError):
            self.close()
            raise RequestError(503, "led-output-unavailable")
        self.power = power
        self.brightness = brightness

    def state(self):
        return {"power": self.power, "brightness": self.brightness}

    def close(self):
        if self.output is not None:
            try:
                self.output.deinit()
            finally:
                self.output = None
                self.pin.init(Pin.OUT, value=0)
        self.power = False


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


def read_command(client, headers, body, deadline):
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
    if type(command) is not dict or len(command) != 2 or "value" not in command:
        raise RequestError(400, "invalid-command")
    return command


def route(client, manifest, expected_token, light):
    deadline = ticks_add(ticks_ms(), REQUEST_TIMEOUT_MS)
    method, path, headers, body = read_headers(client, deadline)
    if method == b"GET" and path == ROUTE_PREFIX + b"manifest":
        return manifest
    read = method == b"GET" and path == ROUTE_PREFIX + b"state"
    write = method == b"POST" and path == ROUTE_PREFIX + b"command"
    if not (read or write):
        raise RequestError(404, "not-found")
    if not authorized(headers.get(b"authorization"), expected_token):
        raise RequestError(401, "token-required")
    if write:
        command = read_command(client, headers, body, deadline)
        capability = command.get("capability")
        value = command["value"]
        if capability == "power" and type(value) is bool:
            light.set(value, light.brightness)
        elif capability == "brightness" and type(value) is int and 0 <= value <= 100:
            light.set(light.power, value)
        else:
            raise RequestError(400, "invalid-capability-or-value")
        print("Light state: power=%s, brightness=%d" % ("on" if light.power else "off", light.brightness))
    return light.state()


def serve_client(client, manifest, expected_token, light):
    try:
        try:
            result = route(client, manifest, expected_token, light)
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
        "capabilities": [
            {"id": "power", "type": "switch", "label": "Питание"},
            {"id": "brightness", "type": "slider", "label": "Яркость", "min": 0, "max": 100,
             "step": 1, "unit": "%"},
        ],
    }


def run():
    if type(LED_GPIO) is not int or LED_GPIO < 0:
        print("Set LED_GPIO to an available output on your ESP32 board")
        return
    if not WIFI_SSID or not 32 <= len(DEVICE_TOKEN) <= 128 or any(
            character not in TOKEN_CHARACTERS for character in DEVICE_TOKEN):
        print("Configure Wi-Fi and a 32-128 character device token in kontur_config.py")
        return
    light = Light(LED_GPIO)
    # The original Arduino sketch formats its eFuse MAC as a little-endian integer.
    device_id = "light-" + "".join("%02x" % part for part in reversed(unique_id()))
    manifest = manifest_for(device_id)
    expected_token = b"Bearer " + DEVICE_TOKEN.encode("ascii")
    wlan = WLAN(getattr(WLAN, "IF_STA", 0))
    listener = None
    discovery = None
    active_ip = None
    retry_at = ticks_ms()
    waiting_at = ticks_add(ticks_ms(), 15000)
    try:
        wlan.active(True)
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        print("Connecting %s to Wi-Fi" % device_id)
        while True:
            now = ticks_ms()
            ip = wlan.ifconfig()[0] if wlan.isconnected() else None
            if ip != active_ip:
                if discovery is not None:
                    discovery.close()
                    discovery = None
                if listener is not None:
                    listener.close()
                    listener = None
                    light.set(False, light.brightness)
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
            if listener is not None:
                try:
                    client, _ = listener.accept()
                except OSError:
                    pass
                else:
                    serve_client(client, manifest, expected_token, light)
            sleep_ms(10)
    finally:
        light.close()
        if discovery is not None:
            discovery.close()
        if listener is not None:
            listener.close()


if __name__ == "__main__":
    run()
