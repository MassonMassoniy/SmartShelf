#include <Arduino.h>
#include <Wire.h>
#include "Adafruit_SHT4x.h"

// put function declarations here:
int myFunction(int, int);

Adafruit_SHT4x sht4 = Adafruit_SHT4x();

void setup() {
  // put your setup code here, to run once:
  Serial.begin(115200);
  unsigned long serial_wait_start = millis();
  while (!Serial && (millis() - serial_wait_start) < 2000) {
    delay(10);
  }
  delay(10000); // small settle for host serial monitor
  Serial.println("Setup started");

  Wire.begin(21, 22); // SDA = GPIO21, SCL = GPIO22

  if (!sht4.begin()) {
    Serial.println("SHT4x not found");
    while (1) delay(10);
  }

  Serial.println("SHT4x found");
  // Optional: set repeatability and heater: SHT4X_HIGH_PRECISION / MED / LOW
  sht4.setPrecision(SHT4X_HIGH_PRECISION);

}

void loop() {
  // put your main code here, to run repeatedly:
  sensors_event_t humidity, temp;
  if (sht4.getEvent(&humidity, &temp)) {
    Serial.print("Temp: ");
    Serial.print(temp.temperature);
    Serial.print(" °C  Humidity: ");
    Serial.print(humidity.relative_humidity);
    Serial.println(" %");
  } else {
    Serial.println("Failed to read SHT4x");
  }
  Serial.println("");

  delay(2000);
}