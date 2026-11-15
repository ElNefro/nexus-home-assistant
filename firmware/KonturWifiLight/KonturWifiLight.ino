#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <WiFi.h>
#include <cstdio>
#include <driver/gpio.h>

#include "secrets.h"

#ifndef KONTUR_LED_GPIO
#define KONTUR_LED_GPIO -1
#endif

namespace {
constexpr int kLedGpio = KONTUR_LED_GPIO;
constexpr uint32_t kLedFrequency = 5000;
constexpr uint8_t kLedResolution = 8;
constexpr uint32_t kLedMaxDuty = (1u << kLedResolution) - 1;
constexpr uint16_t kHttpPort = 80;
constexpr uint32_t kMdnsRetryMs = 5000;
constexpr size_t kMaxCommandBytes = 256;
constexpr char kJson[] = "application/json; charset=utf-8";
constexpr char kManifestPath[] = "/kontur/v1/manifest";
constexpr char kStatePath[] = "/kontur/v1/state";
constexpr char kCommandPath[] = "/kontur/v1/command";
constexpr char kDeviceName[] = "Светильник ESP32";
constexpr char kCapabilityManifest[] = R"json([
  {"id":"power","type":"switch","label":"Питание"},
  {"id":"brightness","type":"slider","label":"Яркость","min":0,"max":100,"step":1,"unit":"%"}
])json";
WebServer server(kHttpPort);
char deviceId[sizeof("light-000000000000")];
bool power = false;
int brightness = 50;
bool ledReady = false;
bool httpStarted = false;
bool mdnsStarted = false;
bool configurationValid = false;
uint32_t mdnsAttemptedAt = 0;

bool validLedPin(int pin) {
  return pin >= 0 && pin < GPIO_NUM_MAX && GPIO_IS_VALID_OUTPUT_GPIO(pin);
}

bool tokenConfigured() {
  const String token(kDeviceToken);
  if (token.length() < 32 || token.length() > 128) return false;
  for (size_t index = 0; index < token.length(); ++index) {
    const char ch = token[index];
    if (!(ch >= 'a' && ch <= 'z') && !(ch >= 'A' && ch <= 'Z') &&
        !(ch >= '0' && ch <= '9') && ch != '-' && ch != '_') return false;
  }
  return true;
}

bool authorized() {
  const String received = server.header("Authorization");
  constexpr char prefix[] = "Bearer ";
  constexpr size_t prefixLength = sizeof(prefix) - 1;
  constexpr size_t tokenLength = sizeof(kDeviceToken) - 1;
  if (received.length() != prefixLength + tokenLength || !received.startsWith(prefix)) return false;

  uint8_t difference = 0;
  for (size_t index = 0; index < tokenLength; ++index) {
    difference |= received[prefixLength + index] ^ kDeviceToken[index];
  }
  return difference == 0;
}

void sendJson(int status, const String& body) {
  server.sendHeader("Cache-Control", "no-store");
  server.send(status, kJson, body);
}

void holdLedLow() {
  // Set the output latch before enabling the output driver.
  gpio_set_level(static_cast<gpio_num_t>(kLedGpio), 0);
  pinMode(kLedGpio, OUTPUT);
}

bool setLight(bool enabled, int percent) {
  if (!ledReady) return false;
  const uint32_t duty = enabled ? (percent * kLedMaxDuty + 50u) / 100u : 0u;
  if (!ledcWrite(kLedGpio, duty)) {
    ledcDetach(kLedGpio);
    holdLedLow();
    ledReady = false;
    power = false;
    Serial.println("LED PWM failed. Output disabled until restart.");
    return false;
  }
  power = enabled;
  brightness = percent;
  return true;
}

void sendState() {
  char body[64];
  std::snprintf(body, sizeof(body), "{\"power\":%s,\"brightness\":%d}", power ? "true" : "false", brightness);
  sendJson(200, body);
}

void handleState() {
  if (!authorized()) {
    sendJson(401, "{\"error\":\"token-required\"}");
    return;
  }
  sendState();
}

void sendManifest() {
  String body = "{\"schemaVersion\":1,\"id\":\"";
  body += deviceId;
  body += "\",\"name\":\"";
  body += kDeviceName;
  body += "\",\"kind\":\"light\",\"requiresToken\":true,\"capabilities\":";
  body += kCapabilityManifest;
  body += '}';
  sendJson(200, body);
}

void handleCommand() {
  if (!authorized()) {
    sendJson(401, "{\"error\":\"token-required\"}");
    return;
  }

  const int contentLength = server.clientContentLength();
  if (contentLength > static_cast<int>(kMaxCommandBytes)) {
    sendJson(413, "{\"error\":\"command-too-large\"}");
    return;
  }

  const String contentType = server.header("Content-Type");
  if (contentType != "application/json" && !contentType.startsWith("application/json;")) {
    sendJson(415, "{\"error\":\"json-required\"}");
    return;
  }
  const String body = server.arg("plain");
  if (contentLength <= 0 || body.length() != static_cast<size_t>(contentLength) || server.args() != 1) {
    sendJson(400, "{\"error\":\"invalid-command-body\"}");
    return;
  }

  JsonDocument command;
  if (deserializeJson(command, body, DeserializationOption::NestingLimit(2)) ||
      !command.is<JsonObject>() || command.size() != 2) {
    sendJson(400, "{\"error\":\"invalid-command\"}");
    return;
  }

  const JsonVariantConst value = command["value"];
  bool nextPower = power;
  int nextBrightness = brightness;
  if (command["capability"] == "power" && value.is<bool>()) {
    nextPower = value.as<bool>();
  } else if (command["capability"] == "brightness" && value.is<int>() &&
             value.as<int>() >= 0 && value.as<int>() <= 100) {
    nextBrightness = value.as<int>();
  } else {
    sendJson(400, "{\"error\":\"invalid-capability-or-value\"}");
    return;
  }
  if (!setLight(nextPower, nextBrightness)) {
    sendJson(503, "{\"error\":\"led-output-unavailable\"}");
    return;
  }
  Serial.printf("Light state: power=%s, brightness=%d\n", power ? "on" : "off", brightness);
  sendState();
}

void startMdns() {
  mdnsAttemptedAt = millis();
  if (!MDNS.begin(deviceId)) {
    Serial.println("mDNS initialization failed");
    return;
  }
  MDNS.setInstanceName(deviceId);
  if (!MDNS.addService("kontur", "tcp", kHttpPort)) {
    Serial.println("Could not announce _kontur._tcp");
    MDNS.end();
    return;
  }
  MDNS.addServiceTxt("kontur", "tcp", "version", "1");
  MDNS.addServiceTxt("kontur", "tcp", "id", deviceId);
  MDNS.addServiceTxt("kontur", "tcp", "kind", "light");
  mdnsStarted = true;
  Serial.printf("Announcing %s.local via _kontur._tcp\n", deviceId);
}

void maintainNetwork() {
  if (WiFi.status() != WL_CONNECTED) {
    if (mdnsStarted) {
      MDNS.end();
      mdnsStarted = false;
    }
    if (httpStarted) {
      server.stop();
      httpStarted = false;
      setLight(false, brightness);
      Serial.println("Wi-Fi disconnected");
    }
    return;
  }

  if (!httpStarted) {
    server.begin();
    httpStarted = true;
    Serial.print("HTTP address: http://");
    Serial.println(WiFi.localIP());
    startMdns();
  } else if (!mdnsStarted && millis() - mdnsAttemptedAt >= kMdnsRetryMs) {
    startMdns();
  }
  server.handleClient();
}
}  // namespace

void setup() {
  Serial.begin(115200);
  if (!validLedPin(kLedGpio)) {
    Serial.println("Set KONTUR_LED_GPIO to an available output on your board in secrets.h");
    return;
  }
  holdLedLow();
  if (!kWifiSsid[0] || !tokenConfigured()) {
    Serial.println("Set Wi-Fi SSID and a 32-128 character device token in secrets.h");
    return;
  }
  ledReady = ledcAttach(kLedGpio, kLedFrequency, kLedResolution);
  if (!ledReady) {
    holdLedLow();
    Serial.println("Unable to initialize LED PWM");
    return;
  }
  if (!setLight(false, brightness)) return;
  std::snprintf(deviceId, sizeof(deviceId), "light-%012llx",
    static_cast<unsigned long long>(ESP.getEfuseMac() & 0xffffffffffffULL));
  const char* headers[] = {"Content-Type"};
  server.collectHeaders(headers, sizeof(headers) / sizeof(headers[0]));
  server.on(kManifestPath, HTTP_GET, sendManifest);
  server.on(kStatePath, HTTP_GET, handleState);
  server.on(kCommandPath, HTTP_POST, handleCommand);
  server.onNotFound([]() { sendJson(404, "{\"error\":\"not-found\"}"); });

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(kWifiSsid, kWifiPassword);
  configurationValid = true;
  Serial.printf("Connecting %s to Wi-Fi\n", deviceId);
}

void loop() {
  if (configurationValid) maintainNetwork();
  delay(5);
}
