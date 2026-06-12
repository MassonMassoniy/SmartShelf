/*
  ESP32-S3 BLE peripheral: expose current time as a characteristic
  Device name: "ESP32-Date"
  Service UUID: 12345678-1234-1234-1234-1234567890ab
  Characteristic UUID: abcdefab-1234-1234-1234-abcdefabcdef

  Replace WiFi NTP servers if desired. This uses NTP to keep time.
*/

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLEAdvertising.h>
#include <time.h>

// BLE UUIDs
static BLEUUID serviceUUID("12345678-1234-1234-1234-1234567890ab");
static BLEUUID charUUID("abcdefab-1234-1234-1234-abcdefabcdef");

BLECharacteristic *pCharacteristic;

const long gmtOffset_sec = 0;
const int daylightOffset_sec = 0;

String iso8601(struct tm *tm) {
  char buf[40];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", tm);
  return String(buf);
}

void setup() {
  Serial.begin(115200);
  delay(100);

  // start NTP
  configTime(gmtOffset_sec, daylightOffset_sec, "pool.ntp.org", "time.nist.gov");
  Serial.println("Waiting for initial time sync...");
  struct tm timeinfo;
  int waitCount = 0;
  while (!getLocalTime(&timeinfo) && waitCount < 20) {
    delay(500);
    Serial.print('.');
    waitCount++;
  }
  Serial.println();

  // init BLE
  BLEDevice::init("ESP32-Date");
  BLEServer *pServer = BLEDevice::createServer();
  BLEService *pService = pServer->createService(serviceUUID);

  pCharacteristic = pService->createCharacteristic(
      charUUID,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY
  );

  pCharacteristic->setValue("unknown");
  pService->start();

  BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(serviceUUID);
  pAdvertising->start();
  Serial.println("BLE advertising as 'ESP32-Date'");
}

void loop() {
  struct tm timeinfo;
  if (getLocalTime(&timeinfo)) {
    String date = iso8601(&timeinfo);
    pCharacteristic->setValue(date.c_str());
    pCharacteristic->notify();
    Serial.println("Notified: " + date);
  } else {
    Serial.println("Time not available yet");
  }
  delay(60000);
}
