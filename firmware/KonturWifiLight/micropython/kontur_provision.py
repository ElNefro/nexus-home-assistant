"""Bluetooth Wi-Fi setup for an unprovisioned Kontur ESP32."""

import binascii
import bluetooth
import cryptolib
import hashlib
import json
import os
from time import ticks_add, ticks_diff, ticks_ms


SERVICE = bluetooth.UUID("7ac20001-9f61-4e58-9d61-67d571076102")
INFO = bluetooth.UUID("7ac20002-9f61-4e58-9d61-67d571076102")
WRITE = bluetooth.UUID("7ac20003-9f61-4e58-9d61-67d571076102")
STATUS = bluetooth.UUID("7ac20004-9f61-4e58-9d61-67d571076102")
IRQ_CENTRAL_CONNECT = 1
IRQ_CENTRAL_DISCONNECT = 2
IRQ_GATTS_WRITE = 3

WIFI_PATH = "/kontur-wifi.json"
WIFI_TEMP_PATH = "/kontur-wifi.tmp"
MAX_WIFI_FILE = 384
MAX_FRAME = 768
CHALLENGE_MS = 120000
WIFI_CONNECT_MS = 20000
ERROR_VISIBLE_MS = 1600
SUCCESS_VISIBLE_MS = 6000
HEX = "0123456789abcdef"


def decode_hex(value, minimum, maximum):
    if (type(value) is not str or not minimum * 2 <= len(value) <= maximum * 2
            or len(value) % 2 or any(char not in HEX for char in value)):
        raise ValueError("invalid encoded field")
    return binascii.unhexlify(value)


def hmac_sha256(key, message):
    # All callers use 32-byte keys. SHA-256 has a 64-byte compression block.
    padded = key + bytes(64 - len(key))
    inner = hashlib.sha256(bytes(value ^ 0x36 for value in padded) + message).digest()
    return hashlib.sha256(bytes(value ^ 0x5c for value in padded) + inner).digest()


def same_mac(left, right):
    difference = 0
    for left_byte, right_byte in zip(left, right):
        difference |= left_byte ^ right_byte
    return difference == 0


def wifi_fields(value):
    if type(value) is not dict or len(value) != 2:
        raise ValueError("invalid Wi-Fi payload")
    ssid, password = value.get("ssid"), value.get("password")
    if type(ssid) is not str or type(password) is not str:
        raise ValueError("invalid Wi-Fi fields")
    ssid_size, password_size = len(ssid.encode()), len(password.encode())
    if not 1 <= ssid_size <= 32 or any(ord(char) < 32 or ord(char) == 127 for char in ssid + password):
        raise ValueError("invalid SSID or password characters")
    if password_size not in (0, 64) and not 8 <= password_size <= 63:
        raise ValueError("invalid Wi-Fi password length")
    if password_size == 64 and any(char not in HEX and char not in "ABCDEF" for char in password):
        raise ValueError("invalid Wi-Fi password format")
    return ssid, password


def decrypt_credentials(frame, key, nonce):
    envelope = json.loads(frame)
    if type(envelope) is not dict or len(envelope) != 3:
        raise ValueError("invalid envelope")
    iv = decode_hex(envelope.get("iv"), 16, 16)
    cipher = decode_hex(envelope.get("data"), 16, 256)
    mac = decode_hex(envelope.get("mac"), 32, 32)
    if len(cipher) % 16:
        raise ValueError("invalid cipher length")
    nonce_bytes = nonce.encode()
    mac_key = hmac_sha256(key, b"kontur:mac:" + nonce_bytes)
    if not same_mac(mac, hmac_sha256(mac_key, iv + cipher)):
        raise ValueError("invalid setup proof")

    enc_key = hmac_sha256(key, b"kontur:enc:" + nonce_bytes)
    plain = cryptolib.aes(enc_key, 2, iv).decrypt(cipher)
    padding = plain[-1]
    if not 1 <= padding <= 16 or plain[-padding:] != bytes([padding]) * padding:
        raise ValueError("invalid padding")
    return wifi_fields(json.loads(plain[:-padding]))


def load_wifi():
    try:
        with open(WIFI_PATH, "rb") as file:
            raw = file.read(MAX_WIFI_FILE + 1)
    except OSError as error:
        if error.args and error.args[0] == 2:
            return None
        raise
    if len(raw) <= MAX_WIFI_FILE:
        try:
            return wifi_fields(json.loads(raw))
        except ValueError:
            pass
    return None


def save_wifi(credentials):
    ssid, password = credentials
    payload = json.dumps({"ssid": ssid, "password": password}).encode()
    with open(WIFI_TEMP_PATH, "wb") as file:
        if file.write(payload) != len(payload):
            raise OSError("incomplete Wi-Fi settings write")
    os.rename(WIFI_TEMP_PATH, WIFI_PATH)


class Provisioner:
    def __init__(self, device_id, name, setup_key):
        self.key = decode_hex(setup_key, 32, 32)
        self.device_id = device_id
        self.name = name
        self.ble = bluetooth.BLE()
        self.connection = None
        self.new_connection = False
        self.lost_connection = False
        self.write_pending = False
        self.ble.active(True)
        try:
            self.ble.config(gap_name="Kontur")
            service = (SERVICE, (
                (INFO, bluetooth.FLAG_READ),
                (WRITE, bluetooth.FLAG_WRITE),
                (STATUS, bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY),
            ))
            ((self.info_handle, self.write_handle, self.status_handle),) = self.ble.gatts_register_services((service,))
            self.ble.gatts_set_buffer(self.info_handle, 160)
            self.ble.gatts_set_buffer(self.write_handle, MAX_FRAME, True)
            self.ble.irq(self._irq)
            self.advertisement = b"\x02\x01\x06\x11\x07" + bytes(SERVICE) + b"\x07\x09Kontur"
            self.frame = bytearray()
            self.credentials = None
            self._new_challenge(ticks_ms())
            self._advertise()
        except BaseException:
            self.close()
            raise

    def _irq(self, event, data):
        if event == IRQ_CENTRAL_CONNECT:
            self.connection = data[0]
            self.new_connection = True
        elif event == IRQ_CENTRAL_DISCONNECT and data[0] == self.connection:
            self.connection = None
            self.lost_connection = True
        elif event == IRQ_GATTS_WRITE and data[0] == self.connection and data[1] == self.write_handle:
            self.write_pending = True

    def _advertise(self):
        self.ble.gap_advertise(250000, adv_data=self.advertisement)

    def _publish(self, status):
        self.status = status
        packet = status.encode()
        self.ble.gatts_write(self.status_handle, packet)
        if self.connection is not None:
            try:
                self.ble.gatts_notify(self.connection, self.status_handle, packet)
            except OSError:
                pass  # The central may have just disconnected.

    def _new_challenge(self, now):
        self.write_pending = False
        self.ble.gatts_read(self.write_handle)
        self.frame = bytearray()
        self.nonce = binascii.hexlify(os.urandom(16)).decode()
        self.deadline = ticks_add(now, CHALLENGE_MS)
        info = json.dumps({"id": self.device_id, "name": self.name, "nonce": self.nonce}).encode()
        self.ble.gatts_write(self.info_handle, info)
        self._publish("ready")

    def _error(self, status, now):
        self.credentials = None
        self.deadline = ticks_add(now, ERROR_VISIBLE_MS)
        self._publish(status)

    def _connect(self, wlan, now):
        nonce, self.nonce = self.nonce, None
        try:
            self.credentials = decrypt_credentials(bytes(self.frame[:-1]), self.key, nonce)
        except (ValueError, TypeError):
            self._error("invalid-code", now)
            return
        finally:
            self.frame = bytearray()
        try:
            wlan.active(True)
            wlan.config(reconnects=2)
            wlan.connect(*self.credentials)
        except (OSError, ValueError):
            self._wifi_failure(wlan, now)
            return
        self.deadline = ticks_add(now, WIFI_CONNECT_MS)
        self._publish("connecting")

    def _receive(self, wlan, now):
        self.write_pending = False
        chunk = self.ble.gatts_read(self.write_handle)
        if self.status != "ready" or self.connection is None or not chunk:
            return
        if len(self.frame) + len(chunk) <= MAX_FRAME:
            self.frame.extend(chunk)
            end = self.frame.find(b"\n")
            if end == len(self.frame) - 1:
                self._connect(wlan, now)
                return
            if end < 0 and len(self.frame) < MAX_FRAME:
                return
        self.nonce = None
        self.frame = bytearray()
        self._error("invalid-code", now)

    def poll(self, wlan):
        now = ticks_ms()
        if self.new_connection:
            self.new_connection = False
            if self.status == "ready":
                self.deadline = ticks_add(now, CHALLENGE_MS)
        if self.status == "connecting":
            if wlan.isconnected() and wlan.ifconfig()[0] != "0.0.0.0":
                try:
                    save_wifi(self.credentials)
                except OSError:
                    self._wifi_failure(wlan, now)
                    return False
                wlan.config(reconnects=-1)
                self.credentials = None
                self.deadline = ticks_add(now, SUCCESS_VISIBLE_MS)
                self._publish("connected")
                return True
            if ticks_diff(now, self.deadline) >= 0:
                self._wifi_failure(wlan, now)
            return False

        if self.lost_connection:
            self.lost_connection = False
            self._new_challenge(now)
            self._advertise()
        elif ticks_diff(now, self.deadline) >= 0:
            if self.status == "ready" and self.connection is not None:
                try:
                    self.ble.gap_disconnect(self.connection)
                except OSError:
                    self.connection = None
                    self.lost_connection = True
                # Wait for disconnect before advertising the next challenge.
                return False
            self._new_challenge(now)
        if self.write_pending:
            self._receive(wlan, now)
        return False

    def _wifi_failure(self, wlan, now):
        try:
            wlan.active(False)
        except OSError:
            pass
        self._error("wifi-error", now)

    def finished(self, now):
        return self.connection is None or ticks_diff(now, self.deadline) >= 0

    def close(self):
        self.ble.active(False)
