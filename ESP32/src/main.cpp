/*
  I2C Bus Scanner — XIAO ESP32-S3
  Scans all 127 addresses and prints which ones respond.
  SGP41 should appear at 0x59.
*/

#include <Arduino.h>
#include <Wire.h>

static const int PIN_SDA = 5;  // D4
static const int PIN_SCL = 6;  // D5

static void printBootBanner() {
    Serial.println(F("\n[boot] I2C scanner starting..."));
    Serial.println(F("[boot] Waiting for serial monitor + scanning I2C"));
}

static void scanI2CBus() {
    Serial.println(F("\n=== I2C Scanner ==="));
    Serial.print(F("SDA: GPIO")); Serial.print(PIN_SDA);
    Serial.print(F("  SCL: GPIO")); Serial.println(PIN_SCL);
    Serial.println(F("Scanning 0x01..0x7E"));
    Serial.println();

    uint8_t found = 0;
    for (uint8_t addr = 0x44; addr < 0x6A; addr++) {
        // Progress marker so hangs are visible in monitor.
        if ((addr % 16) == 1) {
            Serial.print(F("  0x"));
            if (addr < 0x10) Serial.print('0');
            Serial.print(addr, HEX);
            Serial.print(F(": "));
        }

        Wire.beginTransmission(addr);
        uint8_t err = Wire.endTransmission();
        if (err == 0) {
            Serial.print(F("  Device found at 0x"));
            if (addr < 0x10) Serial.print('0');
            Serial.print(addr, HEX);
            if (addr == 0x59) Serial.print(F("  <-- SGP41"));
            Serial.println();
            found++;
        } else if (err == 4) {
            Serial.print(F("  Unknown error at 0x"));
            if (addr < 0x10) Serial.print('0');
            Serial.println(addr, HEX);
        } else {
            Serial.print('.');
        }

        if ((addr % 16) == 0 || addr == 126) Serial.println();
        delay(1);
    }

    if (found == 0) {
        Serial.println(F("  No devices found."));
        Serial.println(F("  Check: power, GND, SDA/SCL not swapped, pull-ups present."));
    } else {
        Serial.print(F("\n  Total devices found: "));
        Serial.println(found);
    }
    Serial.println(F("\n=== Scan complete ==="));
}

void setup() {
    Serial.begin(115200);
    unsigned long serial_wait_start = millis();
    while (!Serial && (millis() - serial_wait_start) < 2000) {
        delay(10);
    }
    delay(10000); // small settle for host serial monitor  // allow USB stack to come up

    // Give monitor a chance to attach on native USB boards.
    const uint32_t serialWaitStart = millis();
    while (!Serial && (millis() - serialWaitStart) < 5000) {
        delay(10);
    }
    for (uint8_t i = 0; i < 3; i++) {
        printBootBanner();
        delay(80);
    }

    Wire.begin(PIN_SDA, PIN_SCL);
    Wire.setClock(100000);  // 100 kHz for diagnostics/stability
    Wire.setTimeOut(20);    // prevent indefinite block on bus issues

    scanI2CBus();
}

void loop() {
    static uint32_t lastScanMs = 0;
    const uint32_t now = millis();
    if (now - lastScanMs >= 5000) {
        lastScanMs = now;
        Serial.print(F("\n[alive] ms="));
        Serial.println(now);
        Serial.println(F("Re-scanning..."));
        scanI2CBus();
    }
}