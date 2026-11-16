"""Verified HTTPS polling for the Kontur cloud service on ESP32."""

import _thread
import json
import ntptime
import socket
import ssl
from time import gmtime, sleep_ms, ticks_add, ticks_diff, ticks_ms


CLOUD_HOST = "nexus-home-assistant-api.onrender.com"
CLOUD_URL = "https://" + CLOUD_HOST
POLL_INTERVAL_MS = 3000
TIMEOUT_SECONDS = 4
HTTP_DEADLINE_MS = 12000
MAX_HEADERS = 4096
MAX_BODY = 2048
CA_LIMIT = 12288
WORKER_STACK = 12288
RECENT_RESULT_LIMIT = 16
HEX_DIGITS = "0123456789abcdef"
COMMAND_ID_CHARACTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"


class CloudError(Exception):
    pass


def tls_context(ca_file):
    with open(ca_file, "rb") as source:
        ca_bytes = source.read(CA_LIMIT + 1)
    if not ca_bytes or len(ca_bytes) > CA_LIMIT:
        raise CloudError("invalid CA file")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    # The Python ssl wrapper interprets its first argument as a file path.
    context.load_verify_locations(cadata=ca_bytes)
    return context


class ResponseReader:
    def __init__(self, secure, transport, deadline):
        self.secure = secure
        self.transport = transport
        self.deadline = deadline

    def _set_timeout(self):
        remaining = ticks_diff(self.deadline, ticks_ms())
        if remaining <= 0:
            raise CloudError("response timed out")
        self.transport.settimeout(min(TIMEOUT_SECONDS, remaining / 1000))

    def _line(self, limit):
        if limit < 2:
            raise CloudError("response headers too large")
        self._set_timeout()
        line = self.secure.readline(limit)
        if not line or not line.endswith(b"\r\n"):
            raise CloudError("invalid response header")
        return line

    def _read(self, size):
        if not size:
            return b""
        self._set_timeout()
        data = self.secure.read(size)
        if data is None or len(data) != size:
            raise CloudError("incomplete response body")
        return data

    def _chunked_body(self):
        data = bytearray()
        while True:
            length_text = self._line(160)[:-2].split(b";", 1)[0]
            if not length_text or len(length_text) > 8 or not all(
                    48 <= digit <= 57 or 65 <= digit <= 70 or 97 <= digit <= 102
                    for digit in length_text):
                raise CloudError("invalid response chunk")
            length = int(length_text, 16)
            if not length:
                return data
            if len(data) + length > MAX_BODY:
                raise CloudError("response body too large")
            data.extend(self._read(length))
            if self._read(2) != b"\r\n":
                raise CloudError("invalid response chunk")

    def response(self):
        status_line = self._line(256)
        status = status_line.split(None, 2)
        if len(status) < 2 or status[0] not in (b"HTTP/1.0", b"HTTP/1.1"):
            raise CloudError("invalid HTTP status")
        if status[1] != b"200":
            if len(status[1]) == 3 and status[1].isdigit():
                raise CloudError("HTTP %s" % status[1].decode("ascii"))
            raise CloudError("invalid HTTP status")

        important = (b"content-length", b"content-type", b"transfer-encoding", b"content-encoding")
        headers = {}
        remaining = MAX_HEADERS - len(status_line)
        while True:
            line = self._line(min(remaining, 1024))
            remaining -= len(line)
            if line == b"\r\n":
                break
            name, separator, value = line.partition(b":")
            if not separator:
                raise CloudError("invalid response header")
            name = name.lower()
            if name in important:
                if name in headers:
                    raise CloudError("duplicate response header")
                headers[name] = value.strip().lower()
        if headers.get(b"content-type", b"").split(b";", 1)[0].strip() != b"application/json":
            raise CloudError("expected JSON response")
        if headers.get(b"content-encoding", b"identity") != b"identity":
            raise CloudError("unsupported response encoding")
        transfer_encoding = headers.get(b"transfer-encoding")
        if transfer_encoding is not None:
            if transfer_encoding != b"chunked" or b"content-length" in headers:
                raise CloudError("unsupported response framing")
            return self._chunked_body()

        length_text = headers.get(b"content-length")
        if length_text is None:
            self._set_timeout()
            data = self.secure.read(MAX_BODY + 1)
            if not data or len(data) > MAX_BODY:
                raise CloudError("invalid response body length")
            return data
        if not length_text.isdigit() or len(length_text) > 4:
            raise CloudError("invalid response body length")
        length = int(length_text)
        if not 0 < length <= MAX_BODY:
            raise CloudError("invalid response body length")
        return self._read(length)


def poll_server(context, token, manifest, result):
    body = json.dumps({"manifest": manifest, "results": [result] if result else []}).encode("utf-8")
    head = (
        "POST /api/cloud/device/poll HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer %s\r\n"
        "Accept: application/json\r\nAccept-Encoding: identity\r\nContent-Type: application/json\r\n"
        "Content-Length: %d\r\nConnection: close\r\n\r\n"
    ) % (CLOUD_HOST, token, len(body))
    packet = head.encode("ascii") + body
    address = socket.getaddrinfo(CLOUD_HOST, 443, socket.AF_INET, socket.SOCK_STREAM)[0][-1]
    transport = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    secure = None
    try:
        deadline = ticks_add(ticks_ms(), HTTP_DEADLINE_MS)
        transport.settimeout(TIMEOUT_SECONDS)
        transport.connect(address)
        secure = context.wrap_socket(transport, server_hostname=CLOUD_HOST)
        if secure.write(packet) != len(packet):
            raise CloudError("incomplete request")
        raw = ResponseReader(secure, transport, deadline).response()
    finally:
        if secure is not None:
            secure.close()
        else:
            transport.close()

    response = json.loads(bytes(raw))
    if type(response) is not dict or "command" not in response:
        raise CloudError("unexpected poll response")
    command = response["command"]
    if command is None:
        return None
    if type(command) is not dict:
        raise CloudError("invalid command")
    command_id = command.get("id")
    if type(command_id) is not str or not 1 <= len(command_id) <= 96 or any(
            character not in COMMAND_ID_CHARACTERS for character in command_id):
        raise CloudError("invalid command ID")
    return command_id, command.get("capability"), command.get("value")


class CloudClient:
    """The worker owns network I/O. The main loop owns the LED and reports completion."""

    def __init__(self, url, token, ca_file, manifest):
        if url not in (CLOUD_URL, CLOUD_URL + "/"):
            raise ValueError("unexpected cloud URL")
        if type(token) is not str or len(token) != 64 or any(char not in HEX_DIGITS for char in token):
            raise ValueError("invalid cloud token format")
        if type(ca_file) is not str or not ca_file:
            raise ValueError("missing CA path")
        self.token = token
        self.ca_file = ca_file
        self.manifest = manifest
        self.lock = _thread.allocate_lock()
        self.running = True
        self.connected = False
        self.command = None
        self.taken = False
        self.result = None
        self.recent = []
        previous_stack = _thread.stack_size(WORKER_STACK)
        try:
            _thread.start_new_thread(self._worker, ())
        finally:
            _thread.stack_size(previous_stack)

    def set_connected(self, connected):
        with self.lock:
            self.connected = connected
            if not connected and not self.taken:
                self.command = None

    def take_command(self):
        with self.lock:
            if not self.connected or self.taken or self.command is None:
                return None
            self.taken = True
            return self.command

    def complete(self, command_id, ok):
        with self.lock:
            if self.command is not None and self.command[0] == command_id and self.taken:
                self.result = {"id": command_id, "ok": ok}
                self.command = None
                self.taken = False

    def close(self):
        with self.lock:
            self.running = False
            self.connected = False

    def _record_response(self, sent, command):
        with self.lock:
            if sent is not None and self.result == sent:
                self.result = None
                if sent not in self.recent:
                    self.recent.append(sent)
                    if len(self.recent) > RECENT_RESULT_LIMIT:
                        self.recent.pop(0)
            if not self.running or not self.connected or command is None:
                return
            if self.command is not None or self.result is not None:
                return
            for previous in self.recent:
                if previous["id"] == command[0]:
                    self.result = previous
                    return
            self.command = command

    def _worker(self):
        context = None
        clock_ready = False
        successful = False
        delay = POLL_INTERVAL_MS
        retry_at = ticks_ms()
        while True:
            with self.lock:
                if not self.running:
                    return
                connected, result = self.connected, self.result
            now = ticks_ms()
            if not connected or ticks_diff(now, retry_at) < 0:
                sleep_ms(200)
                continue
            try:
                if not clock_ready:
                    ntptime.timeout = 2
                    try:
                        ntptime.settime()
                    except OSError:
                        if gmtime()[0] < 2025:
                            raise CloudError("waiting for a valid UTC clock")
                    if not 2025 <= gmtime()[0] <= 2100:
                        raise CloudError("waiting for a valid UTC clock")
                    clock_ready = True
                if context is None:
                    context = tls_context(self.ca_file)
                command = poll_server(context, self.token, self.manifest, result)
                self._record_response(result, command)
                if not successful:
                    print("Cloud polling connected")
                    successful = True
                if result is not None:
                    print("Cloud command acknowledged")
                delay = POLL_INTERVAL_MS
            except (CloudError, OSError, ValueError, MemoryError) as error:
                # Fresh clock sync on the next retry can repair RTC drift or an invalid date.
                clock_ready = False
                successful = False
                if isinstance(error, CloudError):
                    print("Cloud poll failed:", error)
                elif isinstance(error, OSError) and error.args and type(error.args[0]) is int:
                    print("Cloud network error:", error.args[0])
                else:
                    print("Cloud poll failed:", type(error).__name__)
                delay = min(30000, delay * 2)
            retry_at = ticks_add(ticks_ms(), delay)
