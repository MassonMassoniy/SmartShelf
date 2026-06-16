// /*
//   XIAO ESP32-S3 ADC Voltage Monitor
//   Reads ADC-capable pins and prints raw + estimated voltage.
// */

// #include <Arduino.h>

// struct AdcPin {
//     const char* name;
//     uint8_t gpio;
// };

// // XIAO ESP32-S3 pins that expose ADC capability.
// static const AdcPin ADC_PINS[] = {
//     {"D0/A0", 1},
//     {"D1/A1", 2},
//     {"D2/A2", 3},
//     {"D3/A3", 4},
//     {"D4/A4", 5},
//     {"D5/A5", 6},
//     {"D8/A8", 7},
//     {"D9/A9", 8},
//     {"D10/A10", 9},
//     {"D11/A11", 42},
//     {"D12/A12", 41},
// };

// static const float ADC_VREF = 3.30f;     // estimated full-scale reference
// static const uint16_t ADC_MAX = 4095;    // 12-bit ADC max
// static const uint32_t SAMPLE_PERIOD_MS = 1000;

// static float rawToVolts(int raw) {
//     return (static_cast<float>(raw) / ADC_MAX) * ADC_VREF;
// }

// void setup() {
//     Serial.begin(115200);
//     delay(1500);  // allow native USB CDC to appear

//     analogReadResolution(12);
//     for (const auto& pin : ADC_PINS) {
//         analogSetPinAttenuation(pin.gpio, ADC_11db);
//         pinMode(pin.gpio, INPUT);
//     }

//     Serial.println(F("\n=== ESP32-S3 Voltage Monitor ==="));
//     Serial.println(F("Columns: pin  gpio  raw(0..4095)  volts(approx)"));
//     Serial.println(F("WARNING: keep measured voltage <= 3.3V on GPIO pins."));
// }

// void loop() {
//     static uint32_t lastSampleMs = 0;
//     const uint32_t now = millis();
//     if (now - lastSampleMs < SAMPLE_PERIOD_MS) return;
//     lastSampleMs = now;

//     Serial.print(F("\n[ms="));
//     Serial.print(now);
//     Serial.println(F("]"));

//     for (const auto& pin : ADC_PINS) {
//         const int raw = analogRead(pin.gpio);
//         const float volts = rawToVolts(raw);

//         Serial.print(pin.name);
//         Serial.print(F("  GPIO"));
//         Serial.print(pin.gpio);
//         Serial.print(F("  raw="));
//         Serial.print(raw);
//         Serial.print(F("  V="));
//         Serial.println(volts, 3);
//     }
// }