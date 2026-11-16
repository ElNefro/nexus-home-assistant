"""One-time Wi-Fi setup portal for the Kontur ESP32."""

import binascii
import hashlib
import json
import socket
from network import WLAN
from time import ticks_add, ticks_diff, ticks_ms

from kontur_provision import decode_hex, hmac_sha256, save_wifi, wifi_fields


AP_IP = "192.168.4.1"
AP_HOSTS = (b"192.168.4.1", b"192.168.4.1:80")
AP_ORIGIN = b"http://192.168.4.1"
AP_DURATION_MS = 600000
WIFI_CONNECT_MS = 20000
START_DELAY_MS = 250
PROOF_WAIT_MS = 35000
SHUTDOWN_AFTER_PROOF_MS = 6000
MAX_HEADERS = 3072
MAX_BODY = 768
REQUEST_TIMEOUT_MS = 1400
TICKET_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"


PORTAL_PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Контур</title><style>
:root{color-scheme:dark;font:16px system-ui,sans-serif;background:#111020;color:#f5efff}
*{box-sizing:border-box}body{margin:0;padding:20px}main{max-width:460px;margin:36px auto;padding:28px;background:#211b3b;border:1px solid #433667;border-radius:24px}
h1{margin:0 0 10px;font-size:28px}p{color:#d3cae5;line-height:1.5}label{display:block;margin-top:16px}input,select,button,a{font:inherit}
input,select{display:block;width:100%;background:#17132b;color:inherit;border:1px solid #645486;border-radius:10px;padding:12px;margin-top:6px}
button,a{display:block;width:100%;margin-top:24px;padding:12px;border:0;border-radius:10px;background:#d788fa;color:#1b1029;text-align:center;text-decoration:none;font-weight:700}button:disabled{opacity:.5}
[hidden]{display:none}#status:empty{display:none}#status{margin:18px 0 0;min-height:1.5em}#status[data-error=true]{color:#ffb9cc}
</style></head><body><main>
<h1 id="title">Контур</h1><p id="hint"></p>
<form id="form"><label><span id="device-label"></span><select id="device" required></select></label>
<label><span id="ssid-label"></span><input id="ssid" required maxlength="32" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
<label><span id="password-label"></span><input id="password" type="password" maxlength="64" autocomplete="new-password"></label>
<button id="submit" type="submit" disabled></button></form>
<p id="status" role="status" aria-live="polite"></p><a id="return-link" hidden rel="noreferrer"></a>
<script id="device-data" type="application/json">$KONTUR_DEVICE_DATA$</script>
<script>
(() => {
  const el = id => document.getElementById(id);
  const args = new URLSearchParams(location.hash.slice(1));
  const ticket = args.get('ticket') || '';
  const en = args.get('lang') === 'en';
  const strings = en ? {
    title:'Connect to Wi-Fi', hint:'Enter the name and password of your 2.4 GHz home network.',
    device:'Device', ssid:'Wi-Fi network name', password:'Wi-Fi password', submit:'Connect',
    ticket:'Open the setup link from Kontur again.', connecting:'Connecting to your home Wi-Fi…',
    connected:'Device connected. Wait for your phone to rejoin your usual network, then return to Kontur.',
    returnLink:'Return to Kontur', failed:'Could not connect. Check the network name and password, then try again.',
    link:'The setup network is unavailable. Rejoin it and open this page again.',
    invalid:'Enter up to 32 bytes for the network name. Use 8–63 bytes for the password or leave it empty for an open network.'
  } : {
    title:'Подключение к Wi-Fi', hint:'Введи имя и пароль домашней сети 2,4 ГГц.',
    device:'Прибор', ssid:'Имя сети Wi-Fi', password:'Пароль Wi-Fi', submit:'Подключить',
    ticket:'Заново открой ссылку на подключение из «Контура».', connecting:'Прибор подключается к домашней сети…',
    connected:'Прибор подключён. Дождись, пока телефон вернётся в свою сеть, затем открой «Контур».',
    returnLink:'Вернуться в «Контур»', failed:'Не удалось подключиться. Проверь имя и пароль сети и попробуй ещё раз.',
    link:'Нет связи с сетью прибора. Подключись к ней и снова открой эту страницу.',
    invalid:'В имени сети должно быть не более 32 байт. Для пароля введи 8–63 байта или оставь поле пустым для открытой сети.'
  };
  document.documentElement.lang = en ? 'en' : 'ru';
  document.title = strings.title + ' · Контур';
  const data = JSON.parse(el('device-data').textContent);
  const option = document.createElement('option');
  option.value = data.id;
  option.textContent = data.name;
  el('device').appendChild(option);
  for (const [id,key] of Object.entries({title:'title',hint:'hint','device-label':'device','ssid-label':'ssid','password-label':'password',submit:'submit','return-link':'returnLink'})) el(id).textContent = strings[key];
  const update = (text, error = false) => {
    el('status').textContent = text;
    el('status').dataset.error = String(error);
  };
  const form = el('form');
  const button = el('submit');
  const password = el('password');
  if (!/^[A-Za-z0-9_-]{1,320}\.[A-Za-z0-9_-]{43}$/.test(ticket)) { update(strings.ticket,true); return; }

  async function send(path, values) {
    const cancel = new AbortController();
    const timeout = setTimeout(() => cancel.abort(), 2400);
    try {
      const res = await fetch(path, {method:'POST', mode:'same-origin', cache:'no-store', signal:cancel.signal,
        credentials:'omit', headers:{'Content-Type':'application/json'}, body:JSON.stringify(values)});
      if (!res.ok) throw new Error('Request refused');
      return await res.json();
    } finally { clearTimeout(timeout); }
  }

  async function show(result) {
    const deadline = Date.now() + 45000;
    while (result.state === 'connecting' && Date.now() < deadline) {
      update(strings.connecting);
      await new Promise(resolve => setTimeout(resolve,650));
      try { result = await send('/api/status',{ticket}); }
      catch (_) { /* The ESP32 hotspot may briefly be unavailable as its channel changes. */ }
    }
    if (result.state === 'connected' && /^[0-9a-f]{64}$/.test(result.proof)) {
      const link = el('return-link');
      link.href = 'https://nexus-home-assistant.onrender.com/#page=devices&enroll=' + encodeURIComponent(ticket) +
        '&deviceId=' + encodeURIComponent(data.id) + '&proof=' + result.proof;
      password.value = '';
      form.hidden = true;
      link.hidden = false;
      update(strings.connected);
      const forward = () => location.assign(link.href);
      setTimeout(() => {
        if (navigator.onLine) forward();
        else window.addEventListener('online',forward,{once:true});
      }, 9500);
      return;
    }
    if (result.state === 'wifi-error') update(strings.failed,true);
    if (result.state === 'connecting') update(strings.link,true);
    button.disabled = false;
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    const ssid = el('ssid').value;
    const key = password.value;
    const bytes = new TextEncoder();
    const nameSize = bytes.encode(ssid).length;
    const keySize = bytes.encode(key).length;
    const keyIsValid = keySize === 0 || (keySize >= 8 && keySize <= 63) || /^[0-9a-f]{64}$/i.test(key);
    if (!nameSize || nameSize > 32 || !keyIsValid || /[\u0000-\u001f\u007f]/.test(ssid + key)) { update(strings.invalid,true); return; }
    button.disabled = true;
    update(strings.connecting);
    let result;
    try { result = await send('/api/setup',{ticket,ssid,password:key}); }
    catch (_) { result = {state:'connecting'}; }
    await show(result);
  });
  send('/api/status',{ticket}).then(show).catch(() => {button.disabled = false;});
})();
</script></main></body></html>"""


class PortalError(Exception):
    pass


class PortalExpired(Exception):
    pass


def valid_ticket(ticket):
    if type(ticket) is not str or not 45 <= len(ticket) <= 364:
        return False
    payload, separator, signature = ticket.partition(".")
    return (bool(separator) and 1 <= len(payload) <= 320 and len(signature) == 43
            and all(char in TICKET_CHARS for char in payload + signature))


def enrollment_proof(setup_digest, device_id, ticket):
    message = ("kontur:ap:" + ticket + ":" + device_id).encode("ascii")
    return binascii.hexlify(hmac_sha256(setup_digest, message)).decode("ascii")


def read_chunk(client, size, deadline):
    remaining = ticks_diff(deadline, ticks_ms())
    if remaining <= 0:
        raise PortalError(408, "Request Timeout")
    client.settimeout(remaining / 1000)
    return client.recv(size)


def read_request(client):
    deadline = ticks_add(ticks_ms(), REQUEST_TIMEOUT_MS)
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) >= MAX_HEADERS:
            raise PortalError(413, "Payload Too Large")
        chunk = read_chunk(client, min(512, MAX_HEADERS - len(data)), deadline)
        if not chunk:
            raise PortalError(400, "Bad Request")
        data += chunk
    header, _, body = data.partition(b"\r\n\r\n")
    lines = header.split(b"\r\n")
    request = lines[0].split(b" ")
    if len(request) != 3 or request[2] not in (b"HTTP/1.0", b"HTTP/1.1"):
        raise PortalError(400, "Bad Request")
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        name = name.lower()
        if not separator or not name or name in headers:
            raise PortalError(400, "Bad Request")
        headers[name] = value.strip()
    if headers.get(b"host") not in AP_HOSTS:
        raise PortalError(403, "Forbidden")
    return request[0], request[1], headers, body, deadline


def read_post(client, headers, body, deadline):
    if headers.get(b"origin") != AP_ORIGIN or headers.get(b"sec-fetch-site", b"same-origin") != b"same-origin":
        raise PortalError(403, "Forbidden")
    if headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower() != b"application/json":
        raise PortalError(415, "Unsupported Media Type")
    size = headers.get(b"content-length", b"")
    if b"transfer-encoding" in headers or not size.isdigit():
        raise PortalError(400, "Bad Request")
    if len(size) > 3:
        raise PortalError(413, "Payload Too Large")
    size = int(size)
    if size > MAX_BODY:
        raise PortalError(413, "Payload Too Large")
    if size == 0 or len(body) > size:
        raise PortalError(400, "Bad Request")
    while len(body) < size:
        chunk = read_chunk(client, size - len(body), deadline)
        if not chunk:
            raise PortalError(400, "Bad Request")
        body += chunk
    try:
        message = json.loads(body)
    except ValueError:
        raise PortalError(400, "Bad Request")
    if type(message) is not dict or not valid_ticket(message.get("ticket")):
        raise PortalError(400, "Bad Request")
    return message


class Portal:
    def __init__(self, device_id, name, setup_key, password):
        decode_hex(setup_key, 32, 32)
        if type(password) is not str or not 16 <= len(password) <= 63 or any(not 33 <= ord(char) <= 126 for char in password):
            raise ValueError("SETUP_AP_PASSWORD needs 16–63 visible ASCII characters")
        self.setup_digest = hashlib.sha256(setup_key.lower().encode("ascii")).digest()
        self.device_id = device_id
        self.ssid = "Kontur-" + device_id[-6:].upper()
        data = json.dumps({"id": device_id, "name": name}).replace("<", "\\u003c")
        self.page = PORTAL_PAGE.replace("$KONTUR_DEVICE_DATA$", data).encode()
        self.state = "ready"
        self.ticket = None
        self.proof = None
        self.credentials = None
        self.start_at = None
        self.deadline = None
        self.proof_sent = False
        self.expire_at = ticks_add(ticks_ms(), AP_DURATION_MS)
        self.listener = None
        self.ap = WLAN(getattr(WLAN, "IF_AP", 1))
        try:
            self.ap.active(True)
            security = getattr(WLAN, "SEC_WPA2", 3)
            self.ap.config(ssid=self.ssid, key=password, security=security, max_clients=1)
            if self.ap.config("ssid") != self.ssid or self.ap.config("security") != security:
                raise OSError("WPA2 hotspot configuration failed")
            if self.ap.ifconfig()[0] != AP_IP:
                self.ap.ifconfig((AP_IP, "255.255.255.0", AP_IP, AP_IP))
            if self.ap.ifconfig()[0] != AP_IP:
                raise OSError("Setup hotspot address unavailable")
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind((AP_IP, 80))
            self.listener.listen(2)
            self.listener.setblocking(False)
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            if self.listener is not None:
                self.listener.close()
                self.listener = None
        finally:
            self.ap.active(False)

    def _status(self, ticket):
        if self.ticket is not None and ticket != self.ticket:
            if self.state in ("connecting", "connected"):
                raise PortalError(409, "Conflict")
            return {"state": "ready"}
        result = {"state": self.state}
        if self.state == "connected":
            result["proof"] = self.proof
        return result

    def _setup(self, message):
        ticket = message["ticket"]
        if self.state in ("connecting", "connected"):
            return self._status(ticket)
        if len(message) != 3:
            raise PortalError(400, "Bad Request")
        try:
            self.credentials = wifi_fields({"ssid": message.get("ssid"), "password": message.get("password")})
        except ValueError:
            raise PortalError(400, "Bad Request")
        self.ticket = ticket
        self.state = "connecting"
        self.start_at = ticks_add(ticks_ms(), START_DELAY_MS)
        return self._status(ticket)

    def _response(self, client):
        method, path, headers, body, deadline = read_request(client)
        if method == b"GET" and path == b"/" and not body:
            return "text/html; charset=utf-8", self.page, False
        if method != b"POST" or path not in (b"/api/setup", b"/api/status"):
            raise PortalError(404, "Not Found")
        message = read_post(client, headers, body, deadline)
        if path == b"/api/setup":
            result = self._setup(message)
        elif len(message) == 1:
            result = self._status(message["ticket"])
        else:
            raise PortalError(400, "Bad Request")
        return "application/json", json.dumps(result).encode(), result["state"] == "connected"

    def _serve(self, client, sender):
        try:
            if not sender[0].startswith("192.168.4."):
                return
            try:
                mime, payload, serves_proof = self._response(client)
                status, phrase = 200, "OK"
            except PortalError as error:
                status, phrase = error.args
                mime, payload, serves_proof = "application/json", b'{"error":"request-rejected"}', False
            head = (
                "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                "Connection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
                "Referrer-Policy: same-origin\r\nContent-Security-Policy: default-src 'none'; "
                "script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'\r\n\r\n"
            ) % (status, phrase, mime, len(payload))
            client.settimeout(0.7)
            client.write(head.encode("ascii"))
            if client.write(payload) == len(payload) and serves_proof and not self.proof_sent:
                self.proof_sent = True
                remaining = min(SHUTDOWN_AFTER_PROOF_MS, max(0, ticks_diff(self.deadline, ticks_ms())))
                self.deadline = ticks_add(ticks_ms(), remaining)
        except OSError:
            pass
        finally:
            client.close()

    def _failure(self, wlan):
        try:
            wlan.active(False)
        except OSError:
            pass
        self.state = "wifi-error"
        self.credentials = None

    def _check_wifi(self, wlan, now):
        if self.start_at is not None:
            if ticks_diff(now, self.start_at) < 0:
                return
            self.start_at = None
            self.deadline = ticks_add(now, WIFI_CONNECT_MS)
            try:
                wlan.active(True)
                wlan.config(reconnects=2)
                wlan.connect(*self.credentials)
            except (OSError, ValueError):
                self._failure(wlan)
        elif wlan.isconnected() and wlan.ifconfig()[0] != "0.0.0.0":
            try:
                save_wifi(self.credentials)
            except OSError:
                self._failure(wlan)
                return
            wlan.config(reconnects=-1)
            self.credentials = None
            self.proof = enrollment_proof(self.setup_digest, self.device_id, self.ticket)
            self.state = "connected"
            self.deadline = ticks_add(now, PROOF_WAIT_MS)
            print("Wi-Fi setup validated and saved")
        elif ticks_diff(now, self.deadline) >= 0:
            self._failure(wlan)

    def poll(self, wlan):
        now = ticks_ms()
        if self.state == "connecting":
            self._check_wifi(wlan, now)
        elif self.state == "connected" and ticks_diff(now, self.deadline) >= 0:
            return True
        elif self.state in ("ready", "wifi-error") and ticks_diff(now, self.expire_at) >= 0:
            raise PortalExpired()
        try:
            client, sender = self.listener.accept()
        except OSError:
            return False
        self._serve(client, sender)
        return False
