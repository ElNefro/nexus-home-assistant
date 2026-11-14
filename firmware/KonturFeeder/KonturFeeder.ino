#include <WiFi.h>
#include <ESPmDNS.h>
#include <WebServer.h>

// Fill in locally before uploading.
constexpr char kWifiSsid[] = "";
constexpr char kWifiPassword[] = "";
constexpr char kDeviceKind[] = "feeder";
constexpr uint16_t kPort = 80;

WebServer server(kPort);
char deviceId[13];
char hostname[24];
bool announced = false;
unsigned long lastAttempt = 0;

void setup() {
  Serial.begin(115200);
  snprintf(deviceId, sizeof(deviceId), "%012llx",
           static_cast<unsigned long long>(ESP.getEfuseMac() & 0xffffffffffffULL));
  snprintf(hostname, sizeof(hostname), "kontur-%s", deviceId);

  server.on("/identity", HTTP_GET, [] {
    char body[100];
    snprintf(body, sizeof(body), "{\"v\":1,\"kind\":\"%s\",\"id\":\"%s\"}",
             kDeviceKind, deviceId);
    server.send(200, "application/json", body);
  });
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  if (kWifiSsid[0] != '\0') WiFi.begin(kWifiSsid, kWifiPassword);
  else Serial.println("Set kWifiSsid and kWifiPassword before uploading.");
  lastAttempt = millis() - 5000;
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    if (announced) {
      server.stop();
      MDNS.end();
      announced = false;
    }
  } else if (announced) {
    server.handleClient();
  } else if (millis() - lastAttempt >= 5000) {
    lastAttempt = millis();
    if (MDNS.begin(hostname)) {
      server.begin();
      MDNS.addService("kontur", "tcp", kPort);
      MDNS.addServiceTxt("kontur", "tcp", "v", "1");
      MDNS.addServiceTxt("kontur", "tcp", "kind", kDeviceKind);
      MDNS.addServiceTxt("kontur", "tcp", "id", deviceId);
      announced = true;
      Serial.printf("Kontur %s: %s\n", kDeviceKind, WiFi.localIP().toString().c_str());
    }
  }
  delay(5);
}
