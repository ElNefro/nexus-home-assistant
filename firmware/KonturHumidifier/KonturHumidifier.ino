#include <Arduino.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

namespace {
constexpr char kServiceUuid[] = "933f3720-bc7f-4dd1-b631-ab694240dd81";
constexpr char kIdentityUuid[] = "933f3721-bc7f-4dd1-b631-ab694240dd81";
constexpr char kTelemetryUuid[] = "933f3722-bc7f-4dd1-b631-ab694240dd81";
constexpr char kCommandUuid[] = "933f3723-bc7f-4dd1-b631-ab694240dd81";
constexpr char kDeviceName[] = "Увлажнитель";
constexpr uint8_t kVersion = 1;
constexpr uint8_t kSetPower = 1;
constexpr uint8_t kSetTargetHumidity = 2;
constexpr uint32_t kLongPressMs = 10000;
constexpr uint32_t kPairingWindowMs = 120000;

// Set a free input-capable GPIO to use a button wired to ground.
constexpr int kPairingButtonGpio = -1;
constexpr bool kUsePairingButton = kPairingButtonGpio >= 0;

struct Command {
  uint8_t opcode;
  uint8_t value;
};

// Simulated state, updated only by the Arduino loop.
uint8_t power = 0;
uint8_t targetHumidity = 50;
uint8_t humidity = 46;
uint8_t waterLevel = 65;
QueueHandle_t commandQueue;
BLECharacteristic* telemetry;
BLEAdvertising* advertising;
std::atomic<bool> clientConnected{false};
std::atomic<bool> advertisingInterrupted{false};
bool advertisingActive = false;
bool buttonHeld = false;
bool holdHandled = false;
bool pairingWindowActive = false;
uint32_t holdStartedAt = 0;
uint32_t pairingStartedAt = 0;

class ConnectionCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer*) override {
    clientConnected.store(true);
    advertisingInterrupted.store(true);
  }

  void onDisconnect(BLEServer*) override {
    clientConnected.store(false);
  }
};

class CommandCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* characteristic) override {
    if (characteristic->getLength() != 2) return;

    const auto* bytes = characteristic->getData();
    const Command command{bytes[0], bytes[1]};
    const bool validPower = command.opcode == kSetPower && command.value <= 1;
    const bool validTarget = command.opcode == kSetTargetHumidity &&
      command.value >= 30 && command.value <= 70;
    if (validPower || validTarget) {
      xQueueSend(commandQueue, &command, 0);
    }
  }
};

ConnectionCallbacks connectionCallbacks;
CommandCallbacks commandCallbacks;

void publishTelemetry(bool notify) {
  const uint8_t payload[] = {kVersion, power, targetHumidity, humidity, waterLevel};
  telemetry->setValue(payload, sizeof(payload));
  if (notify && clientConnected.load()) telemetry->notify();
}

void updatePairingButton(uint32_t now) {
  if (!kUsePairingButton) return;

  if (digitalRead(kPairingButtonGpio) == HIGH) {
    buttonHeld = false;
    holdHandled = false;
  } else if (!buttonHeld) {
    buttonHeld = true;
    holdStartedAt = now;
  } else if (!holdHandled && now - holdStartedAt >= kLongPressMs) {
    pairingWindowActive = true;
    pairingStartedAt = now;
    holdHandled = true;
  }
  if (pairingWindowActive && now - pairingStartedAt >= kPairingWindowMs) {
    pairingWindowActive = false;
  }
}

void updateAdvertising() {
  // The connectable advertisement stops when the client connects.
  if (advertisingInterrupted.exchange(false)) advertisingActive = false;
  if (clientConnected.load()) return;

  const bool shouldAdvertise = !kUsePairingButton || pairingWindowActive;
  if (shouldAdvertise && !advertisingActive) {
    if (advertising->start()) {
      advertisingActive = true;
      Serial.println("BLE advertising started");
    }
  } else if (!shouldAdvertise && advertisingActive) {
    if (advertising->stop()) {
      advertisingActive = false;
      Serial.println("BLE advertising stopped");
    }
  }
}

bool applySerialLine(const char* line) {
  const char* number = nullptr;
  uint8_t* reading = nullptr;
  if (std::strncmp(line, "humidity ", 9) == 0) {
    number = line + 9;
    reading = &humidity;
  } else if (std::strncmp(line, "water ", 6) == 0) {
    number = line + 6;
    reading = &waterLevel;
  }
  if (!number || !*number) return false;

  unsigned value = 0;
  for (const char* digit = number; *digit; ++digit) {
    if (*digit < '0' || *digit > '9') return false;
    value = value * 10 + (*digit - '0');
    if (value > 100) return false;
  }
  *reading = static_cast<uint8_t>(value);
  return true;
}

void readSerial() {
  static char line[32];
  static size_t length = 0;
  static bool overflow = false;
  // Bound each serial batch so incoming text cannot starve BLE command processing.
  for (size_t count = 0; count < sizeof(line) && Serial.available(); ++count) {
    const int byte = Serial.read();
    if (byte == '\r' || byte == '\n') {
      if (length || overflow) {
        line[length] = '\0';
        if (!overflow && applySerialLine(line)) {
          publishTelemetry(true);
          Serial.println("Telemetry updated");
        } else {
          Serial.println("Use: humidity 0..100 or water 0..100");
        }
        length = 0;
        overflow = false;
      }
    } else if (length < sizeof(line) - 1 && byte != 0) {
      line[length++] = static_cast<char>(byte);
    } else {
      overflow = true;
    }
  }
}
}  // namespace

void setup() {
  Serial.begin(115200);
  if (kUsePairingButton) pinMode(kPairingButtonGpio, INPUT_PULLUP);
  commandQueue = xQueueCreate(8, sizeof(Command));
  if (!commandQueue) {
    Serial.println("Could not allocate BLE command queue");
    return;
  }

  BLEDevice::init(kDeviceName);
  auto* server = BLEDevice::createServer();
  server->setCallbacks(&connectionCallbacks);
  server->advertiseOnDisconnect(false);
  auto* service = server->createService(kServiceUuid);

  auto* identity = service->createCharacteristic(kIdentityUuid, BLECharacteristic::PROPERTY_READ);
  char payload[80];
  std::snprintf(payload, sizeof(payload),
    "{\"v\":1,\"kind\":\"humidifier\",\"id\":\"%012llx\",\"telemetry\":1}",
    static_cast<unsigned long long>(ESP.getEfuseMac() & 0xffffffffffffULL));
  identity->setValue(payload);

  telemetry = service->createCharacteristic(kTelemetryUuid,
    BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
#if defined(CONFIG_BLUEDROID_ENABLED)
  telemetry->addDescriptor(new BLE2902());
#endif
  publishTelemetry(false);
  auto* commands = service->createCharacteristic(kCommandUuid, BLECharacteristic::PROPERTY_WRITE);
  commands->setCallbacks(&commandCallbacks);
  service->start();

  auto* advertiser = BLEDevice::getAdvertising();
  BLEAdvertisementData packet;
  packet.setFlags(0x06);
  packet.setCompleteServices(BLEUUID(kServiceUuid));
  BLEAdvertisementData response;
  response.setName(kDeviceName);
  if (!advertiser->setAdvertisementData(packet) || !advertiser->setScanResponseData(response)) {
    Serial.println("Could not configure BLE advertising data");
    return;
  }
  advertiser->setScanResponse(true);
  advertising = advertiser;
  Serial.println("Kontur humidifier simulator ready");
}

void loop() {
  if (!advertising) {
    delay(1000);
    return;
  }

  updatePairingButton(millis());
  updateAdvertising();
  readSerial();
  Command command;
  if (xQueueReceive(commandQueue, &command, 0) == pdTRUE) {
    if (command.opcode == kSetPower) power = command.value;
    if (command.opcode == kSetTargetHumidity) targetHumidity = command.value;
    publishTelemetry(true);
  }
  delay(15);
}
