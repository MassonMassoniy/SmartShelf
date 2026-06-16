#include <Arduino.h>
#include <Wire.h>
#include <SensirionI2CSgp41.h>
#include <VOCGasIndexAlgorithm.h>
#include <NOxGasIndexAlgorithm.h>

namespace {

constexpr uint8_t PIN_SDA = 5;   // XIAO ESP32-S3 D4
constexpr uint8_t PIN_SCL = 6;   // XIAO ESP32-S3 D5
constexpr uint8_t PIN_VCNL4040_INT = 9;  // XIAO ESP32-S3 D2
constexpr uint32_t LOOP_PERIOD_MS = 100;
constexpr uint32_t SGP41_CONDITIONING_S = 10;

constexpr uint8_t ADDR_SHT45 = 0x44;
constexpr uint8_t ADDR_VCNL4040 = 0x60;
constexpr uint8_t ADDR_SGP41 = 0x59;

constexpr uint8_t REG_VCNL4040_ALS_CONF = 0x00;
constexpr uint8_t REG_VCNL4040_PS_CONF12 = 0x03;
constexpr uint8_t REG_VCNL4040_PS_CONF3MS = 0x04;
constexpr uint8_t REG_VCNL4040_PS_DATA = 0x08;
constexpr uint8_t REG_VCNL4040_ALS_DATA = 0x09;
constexpr uint8_t REG_VCNL4040_WHITE_DATA = 0x0A;
constexpr uint8_t REG_VCNL4040_INT_FLAG = 0x0B;
constexpr uint8_t REG_VCNL4040_ID = 0x0C;
constexpr uint8_t REG_VCNL4040_PS_THDL = 0x06;
constexpr uint8_t REG_VCNL4040_PS_THDH = 0x07;

constexpr uint8_t CMD_SHT45_MEASURE_HIGH_PREC = 0xFD;

SensirionI2CSgp41 sgp41;
VOCGasIndexAlgorithm vocAlgorithm;
NOxGasIndexAlgorithm noxAlgorithm;

uint32_t setupMs = 0;
uint32_t lastLoopMs = 0;

bool sht45Ok = false;
bool vcnl4040Ok = false;
bool sgp41Ok = false;
bool sgp41ConditioningDone = false;
volatile bool vcnlIntPending = false;
volatile uint32_t vcnlIntCount = 0;

constexpr uint16_t VCNL4040_PS_LOW_THRESHOLD = 1200;
constexpr uint16_t VCNL4040_PS_HIGH_THRESHOLD = 2000;

void IRAM_ATTR onVcnl4040Interrupt() {
    vcnlIntPending = true;
    ++vcnlIntCount;
}

uint8_t crc8(const uint8_t* data, uint8_t len) {
    uint8_t crc = 0xFF;
    for (uint8_t i = 0; i < len; ++i) {
        crc ^= data[i];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            if (crc & 0x80) {
                crc = static_cast<uint8_t>((crc << 1) ^ 0x31);
            } else {
                crc <<= 1;
            }
        }
    }
    return crc;
}

bool i2cDevicePresent(uint8_t addr) {
    Wire.beginTransmission(addr);
    return Wire.endTransmission() == 0;
}

bool writeRegister16(uint8_t devAddr, uint8_t reg, uint16_t value) {
    Wire.beginTransmission(devAddr);
    Wire.write(reg);
    Wire.write(static_cast<uint8_t>(value & 0xFF));        // low byte first
    Wire.write(static_cast<uint8_t>((value >> 8) & 0xFF)); // high byte
    return Wire.endTransmission() == 0;
}

bool readRegister16(uint8_t devAddr, uint8_t reg, uint16_t& out) {
    Wire.beginTransmission(devAddr);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) {
        return false;
    }

    const uint8_t expected = 2;
    const uint8_t got = Wire.requestFrom(devAddr, expected);
    if (got != expected) {
        return false;
    }

    const uint8_t lo = Wire.read();
    const uint8_t hi = Wire.read();
    out = static_cast<uint16_t>((static_cast<uint16_t>(hi) << 8) | lo);
    return true;
}

bool readSht45(float& temperatureC, float& humidityRh) {
    Wire.beginTransmission(ADDR_SHT45);
    Wire.write(CMD_SHT45_MEASURE_HIGH_PREC);
    if (Wire.endTransmission() != 0) {
        return false;
    }

    delay(10);  // high precision conversion time

    const uint8_t expected = 6;
    const uint8_t got = Wire.requestFrom(ADDR_SHT45, expected);
    if (got != expected) {
        return false;
    }

    uint8_t data[6];
    for (uint8_t i = 0; i < expected; ++i) {
        data[i] = Wire.read();
    }

    if (crc8(data, 2) != data[2] || crc8(data + 3, 2) != data[5]) {
        return false;
    }

    const uint16_t rawTemp = static_cast<uint16_t>((data[0] << 8) | data[1]);
    const uint16_t rawHum = static_cast<uint16_t>((data[3] << 8) | data[4]);

    temperatureC = -45.0f + 175.0f * (static_cast<float>(rawTemp) / 65535.0f);
    humidityRh = -6.0f + 125.0f * (static_cast<float>(rawHum) / 65535.0f);

    if (humidityRh < 0.0f) humidityRh = 0.0f;
    if (humidityRh > 100.0f) humidityRh = 100.0f;
    return true;
}

uint16_t encodeHumidityTicks(float humidityRh) {
    return static_cast<uint16_t>((humidityRh / 100.0f) * 65535.0f);
}

uint16_t encodeTemperatureTicks(float temperatureC) {
    return static_cast<uint16_t>(((temperatureC + 45.0f) / 175.0f) * 65535.0f);
}

void printHeader() {
    Serial.println(F("\n=== XIAO ESP32-S3 I2C Sensor Test ==="));
    Serial.print(F("I2C pins: SDA=GPIO"));
    Serial.print(PIN_SDA);
    Serial.print(F(" SCL=GPIO"));
    Serial.println(PIN_SCL);
    Serial.println(F("Sensors: SHT45 + VCNL4040 + SGP41"));
    Serial.print(F("VCNL4040 INT pin: GPIO"));
    Serial.println(PIN_VCNL4040_INT);
}

bool initSht45() {
    if (!i2cDevicePresent(ADDR_SHT45)) {
        Serial.println(F("[SHT45] Not detected at 0x44"));
        return false;
    }

    float t = 0.0f;
    float rh = 0.0f;
    if (!readSht45(t, rh)) {
        Serial.println(F("[SHT45] Detected but first measurement failed"));
        return false;
    }

    Serial.print(F("[SHT45] OK  T="));
    Serial.print(t, 2);
    Serial.print(F(" C  RH="));
    Serial.print(rh, 2);
    Serial.println(F(" %"));
    return true;
}

bool initVcnl4040() {
    if (!i2cDevicePresent(ADDR_VCNL4040)) {
        Serial.println(F("[VCNL4040] Not detected at 0x60"));
        return false;
    }

    // Bring ALS/PS blocks out of shutdown using baseline configuration.
    bool ok = true;
    ok &= writeRegister16(ADDR_VCNL4040, REG_VCNL4040_ALS_CONF, 0x0000);
    // PS_INT bits[9:8] = 0b11 => trigger on both close and away.
    ok &= writeRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_CONF12, 0x0300);
    ok &= writeRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_CONF3MS, 0x0000);
    ok &= writeRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_THDL, VCNL4040_PS_LOW_THRESHOLD);
    ok &= writeRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_THDH, VCNL4040_PS_HIGH_THRESHOLD);
    if (!ok) {
        Serial.println(F("[VCNL4040] Configuration write failed"));
        return false;
    }

    uint16_t id = 0;
    uint16_t prox = 0;
    uint16_t als = 0;
    uint16_t white = 0;
    if (!readRegister16(ADDR_VCNL4040, REG_VCNL4040_ID, id) ||
        !readRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_DATA, prox) ||
        !readRegister16(ADDR_VCNL4040, REG_VCNL4040_ALS_DATA, als) ||
        !readRegister16(ADDR_VCNL4040, REG_VCNL4040_WHITE_DATA, white)) {
        Serial.println(F("[VCNL4040] Register read failed"));
        return false;
    }

    Serial.print(F("[VCNL4040] OK  ID=0x"));
    Serial.print(id, HEX);
    Serial.print(F("  Prox="));
    Serial.print(prox);
    Serial.print(F("  ALS="));
    Serial.print(als);
    Serial.print(F("  White="));
    Serial.print(white);
    Serial.print(F("  TH(low/high)="));
    Serial.print(VCNL4040_PS_LOW_THRESHOLD);
    Serial.print('/');
    Serial.println(VCNL4040_PS_HIGH_THRESHOLD);
    return true;
}

bool initSgp41() {
    if (!i2cDevicePresent(ADDR_SGP41)) {
        Serial.println(F("[SGP41] Not detected at 0x59"));
        return false;
    }

    sgp41.begin(Wire);
    uint16_t serial[3] = {};
    uint16_t err = sgp41.getSerialNumber(serial);
    if (err != 0) {
        Serial.print(F("[SGP41] Serial read error 0x"));
        Serial.println(err, HEX);
        return false;
    }

    uint16_t testResult = 0;
    err = sgp41.executeSelfTest(testResult);
    if (err != 0) {
        Serial.print(F("[SGP41] Self-test communication error 0x"));
        Serial.println(err, HEX);
        return false;
    }

    Serial.print(F("[SGP41] OK  SN=0x"));
    for (uint8_t i = 0; i < 3; ++i) {
        if (serial[i] < 0x1000) Serial.print('0');
        Serial.print(serial[i], HEX);
    }
    Serial.print(F("  SelfTest=0x"));
    Serial.println(testResult, HEX);
    return true;
}

}  // namespace

void setup() {
    Serial.begin(115200);
    delay(1500);

    printHeader();

    Wire.begin(PIN_SDA, PIN_SCL);
    Wire.setClock(100000);
    Wire.setTimeOut(20);

    sht45Ok = initSht45();
    vcnl4040Ok = initVcnl4040();
    sgp41Ok = initSgp41();

    if (vcnl4040Ok) {
        pinMode(PIN_VCNL4040_INT, INPUT_PULLUP);
        attachInterrupt(digitalPinToInterrupt(PIN_VCNL4040_INT), onVcnl4040Interrupt, FALLING);
        uint16_t dummyFlags = 0;
        readRegister16(ADDR_VCNL4040, REG_VCNL4040_INT_FLAG, dummyFlags);  // clear stale flags
        Serial.println(F("[VCNL4040] Interrupt enabled (FALLING edge on INT pin)"));
    }

    setupMs = millis();
    lastLoopMs = setupMs - LOOP_PERIOD_MS;

    Serial.println(F("\n--- Loop output every 3 seconds ---"));
}

void loop() {
    const uint32_t now = millis();
    if (now - lastLoopMs < LOOP_PERIOD_MS) {
        return;
    }
    lastLoopMs = now;

    float temperatureC = NAN;
    float humidityRh = NAN;
    uint16_t prox = 0;
    uint16_t als = 0;
    uint16_t white = 0;
    float lux = NAN;
    uint16_t srawVoc = 0;
    uint16_t srawNox = 0;
    int32_t vocIndex = 0;
    int32_t noxIndex = 0;

    bool shtReadOk = false;
    bool vcnlReadOk = false;
    bool sgpReadOk = false;
    bool intEvent = false;
    uint32_t intCountSnapshot = 0;
    uint16_t intFlagsRaw = 0;
    bool intFlagsOk = false;

    if (sht45Ok) {
        shtReadOk = readSht45(temperatureC, humidityRh);
    }

    if (vcnl4040Ok) {
        noInterrupts();
        intEvent = vcnlIntPending;
        if (intEvent) {
            vcnlIntPending = false;
        }
        intCountSnapshot = vcnlIntCount;
        interrupts();

        const bool proxOk = readRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_DATA, prox);
        const bool alsOk = readRegister16(ADDR_VCNL4040, REG_VCNL4040_ALS_DATA, als);
        const bool whiteOk = readRegister16(ADDR_VCNL4040, REG_VCNL4040_WHITE_DATA, white);
        vcnlReadOk = proxOk && alsOk && whiteOk;
        if (vcnlReadOk) {
            lux = static_cast<float>(als) * 0.1f;  // typical VCNL4040 conversion
        }

        if (intEvent) {
            intFlagsOk = readRegister16(ADDR_VCNL4040, REG_VCNL4040_INT_FLAG, intFlagsRaw);
        }
    }

    if (sgp41Ok) {
        const float compT = shtReadOk ? temperatureC : 25.0f;
        const float compRh = shtReadOk ? humidityRh : 50.0f;
        const uint16_t rhTicks = encodeHumidityTicks(compRh);
        const uint16_t tTicks = encodeTemperatureTicks(compT);

        uint16_t err = 0;
        if (!sgp41ConditioningDone && (now - setupMs) < (SGP41_CONDITIONING_S * 1000UL)) {
            err = sgp41.executeConditioning(rhTicks, tTicks, srawVoc);
            if (err == 0) {
                vocAlgorithm.process(srawVoc);
                sgpReadOk = true;
            }
        } else {
            sgp41ConditioningDone = true;
            err = sgp41.measureRawSignals(rhTicks, tTicks, srawVoc, srawNox);
            if (err == 0) {
                vocIndex = vocAlgorithm.process(srawVoc);
                noxIndex = noxAlgorithm.process(srawNox);
                sgpReadOk = true;
            }
        }
    }

    Serial.print(F("[ms="));
    Serial.print(now);
    Serial.println(F("]"));

    Serial.print(F("SHT45: "));
    if (shtReadOk) {
        Serial.print(F("T="));
        Serial.print(temperatureC, 2);
        Serial.print(F(" C, RH="));
        Serial.print(humidityRh, 2);
        Serial.println(F(" %"));
    } else {
        Serial.println(F("read failed"));
    }

    Serial.print(F("VCNL4040: "));
    if (vcnlReadOk) {
        Serial.print(F("Prox="));
        Serial.print(prox);
        Serial.print(F(", ALS="));
        Serial.print(als);
        Serial.print(F(", White="));
        Serial.print(white);
        Serial.print(F(", Lux~"));
        Serial.println(lux, 2);
        if (intEvent) {
            Serial.print(F("  VCNL4040 INT: count="));
            Serial.print(intCountSnapshot);
            if (intFlagsOk) {
                const bool closeEvent = ((intFlagsRaw >> 9) & 0x1) != 0;
                const bool awayEvent = ((intFlagsRaw >> 8) & 0x1) != 0;
                Serial.print(F(" flags=0x"));
                Serial.print(intFlagsRaw, HEX);
                Serial.print(F(" [close="));
                Serial.print(closeEvent ? F("1") : F("0"));
                Serial.print(F(", away="));
                Serial.print(awayEvent ? F("1") : F("0"));
                Serial.println(']');

                Serial.print(F("  VCNL4040 INT EVENT: "));
                if (closeEvent && awayEvent) {
                    Serial.println(F("CLOSE+AWAY"));
                } else if (closeEvent) {
                    Serial.println(F("CLOSE"));
                } else if (awayEvent) {
                    Serial.println(F("AWAY"));
                } else {
                    Serial.println(F("NO_FLAG_SET"));
                }
            } else {
                Serial.println(F(" flags read failed"));
            }
        }
    } else {
        Serial.println(F("read failed"));
    }

    Serial.print(F("SGP41: "));
    if (sgpReadOk) {
        if (!sgp41ConditioningDone && (now - setupMs) < (SGP41_CONDITIONING_S * 1000UL)) {
            Serial.print(F("conditioning, srawVOC="));
            Serial.println(srawVoc);
        } else {
            Serial.print(F("srawVOC="));
            Serial.print(srawVoc);
            Serial.print(F(", srawNOx="));
            Serial.print(srawNox);
            Serial.print(F(", VOC index="));
            Serial.print(vocIndex);
            Serial.print(F(", NOx index="));
            Serial.println(noxIndex);
        }
    } else {
        Serial.println(F("read failed"));
    }

    Serial.println();
}
