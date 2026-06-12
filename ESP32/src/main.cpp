/*
  ESP32-S3: Get time via NTP and POST date to Raspberry Pi HTTP endpoint
  Replace `YOUR_SSID`, `YOUR_PASSWORD` and `RASPBERRY_PI_IP` before uploading.
*/

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>

// --- Configure these ---
const char* ssid = "YOUR_SSID";
const char* password = "YOUR_PASSWORD";
const char* serverUrl = "http://RASPBERRY_PI_IP:5000/date"; // e.g. http://192.168.1.42:5000/date
// -----------------------

// timezone config: seconds east of UTC and minutes of DST -> keep 0 for UTC here
const long gmtOffset_sec = 0;
const int daylightOffset_sec = 0;

String iso8601(struct tm *tm) {
  char buf[40];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", tm);
  return String(buf);
}

void connectWiFi() {
  Serial.printf("Connecting to %s", ssid);
  WiFi.begin(ssid, password);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 40) {
    delay(500);
    Serial.print('.');
    tries++;
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("WiFi connected, IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("WiFi connection failed");
  }
}

void setup() {
  Serial.begin(115200);
  delay(100);
  connectWiFi();

  // configure NTP
  configTime(gmtOffset_sec, daylightOffset_sec, "pool.ntp.org", "time.nist.gov");
  Serial.println("Waiting for time sync...");
  struct tm timeinfo;
  int waitCount = 0;
  while (!getLocalTime(&timeinfo) && waitCount < 20) {
    delay(500);
    Serial.print('.');
    waitCount++;
  }
  Serial.println();
  if (getLocalTime(&timeinfo)) {
    Serial.println("Time synchronized");
  } else {
    Serial.println("Failed to sync time (still continuing) ");
  }
}

void loop() {
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo)) {
    Serial.println("No time available");
  } else {
    String date = iso8601(&timeinfo);
    Serial.println("Sending date: " + date);

    if (WiFi.status() == WL_CONNECTED) {
      HTTPClient http;
      http.begin(serverUrl);
      http.addHeader("Content-Type", "application/json");
      String payload = "{ \"date\": \"" + date + "\" }";
      int httpCode = http.POST(payload);
      if (httpCode > 0) {
        Serial.printf("POST response: %d\n", httpCode);
      } else {
        Serial.printf("POST failed, error: %s\n", http.errorToString(httpCode).c_str());
      }
      http.end();
    } else {
      Serial.println("WiFi not connected, reconnecting...");
      connectWiFi();
    }
  }

  // send once per minute; adjust as needed
  delay(60000);
}