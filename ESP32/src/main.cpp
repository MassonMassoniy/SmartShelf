#include <Arduino.h>
#include <Wire.h>
#include <SensirionI2CSgp41.h>
#include <VOCGasIndexAlgorithm.h>
#include <NOxGasIndexAlgorithm.h>
#include <BLEDevice.h>
#include <cstring>
#include <cmath>
#include <string>

namespace {

constexpr uint8_t PIN_SDA = 5;   // XIAO ESP32-S3 D4
constexpr uint8_t PIN_SCL = 6;   // XIAO ESP32-S3 D5
constexpr uint8_t PIN_VCNL4040_INT = 9;  // XIAO ESP32-S3 D2
constexpr uint32_t LOOP_PERIOD_MS = 1000;
constexpr uint32_t SGP41_CONDITIONING_S = 10;
constexpr uint16_t BLE_COMPANY_ID = 0xFFFF;  // Replace with your own company ID if available.
constexpr uint16_t BLE_ADV_MIN_INTERVAL_UNITS = 320;  // 200 ms
constexpr uint16_t BLE_ADV_MAX_INTERVAL_UNITS = 480;  // 300 ms

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
uint16_t packetSequence = 0;
uint16_t nodeId = 0;
uint64_t mac = 0;
char nodeName[20] = {};
BLEAdvertising* bleAdvertising = nullptr;

constexpr uint16_t VCNL4040_PS_LOW_THRESHOLD = 1200;
constexpr uint16_t VCNL4040_PS_HIGH_THRESHOLD = 2000;
constexpr uint16_t VCNL4040_PS_CONF12_MAX_RANGE = 0x0B0E;  // PS_INT both + 16-bit + duty 1/40 + IT 8T + power on
constexpr uint16_t VCNL4040_PS_CONF3MS_MAX_RANGE = 0x0700; // LED current 200 mA (PS_MS high byte bits[2:0] = 0b111)

struct __attribute__((packed)) SensorBlePayload {
    uint8_t protocolVersion;
    uint16_t nodeId;
    uint8_t flags;               // bit0 SHT45, bit1 VCNL4040, bit2 SGP41
    int16_t temperatureCenti;    // deg C * 100
    uint16_t humidityCenti;      // %RH * 100
    uint16_t proximityRaw;
    int16_t vocIndex;
    uint16_t sequence;
};

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

    // Bring ALS/PS blocks out of shutdown and configure proximity for
    // long-range presence detection sensitivity:
    // - LED current: 200 mA
    // - Duty ratio: 1/40
    // - Integration time: 8T
    // - Proximity high resolution: 16-bit
    bool ok = true;
    ok &= writeRegister16(ADDR_VCNL4040, REG_VCNL4040_ALS_CONF, 0x0000);
    ok &= writeRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_CONF12, VCNL4040_PS_CONF12_MAX_RANGE);
    ok &= writeRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_CONF3MS, VCNL4040_PS_CONF3MS_MAX_RANGE);
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

uint16_t computeNodeIdFromMac() {
    // If `mac` hasn't been populated yet, read efuse MAC as a fallback.
    if (mac == 0) {
        mac = ESP.getEfuseMac();
    }

    // mac is a 48-bit address stored in a uint64_t. Extract the 6 MAC bytes
    // (LSB first). We form the node id from the lowest two bytes but present
    // them in big-endian order so the ID matches the conventional human
    // representation of the last two MAC octets (AA:BB -> 0xAABB).
    uint8_t macBytes[6];
    for (int i = 0; i < 6; ++i) {
        macBytes[i] = static_cast<uint8_t>((mac >> (8 * i)) & 0xFF);
    }

    uint16_t node = static_cast<uint16_t>((static_cast<uint16_t>(macBytes[1]) << 8) |
                                          static_cast<uint16_t>(macBytes[0]));
    return node;
}

void buildNodeName(char* outName, size_t outLen) {
    const uint64_t mac = ESP.getEfuseMac();
    const uint32_t macTail = static_cast<uint32_t>(mac & 0xFFFFFF);
    snprintf(outName, outLen, "SmartShelf-%06lX", static_cast<unsigned long>(macTail));
}

void initBleTransport() {
    // Build a human-friendly node name from the efuse MAC first.
    buildNodeName(nodeName, sizeof(nodeName));

    // Initialize BLE with that device name.
    BLEDevice::init(nodeName);
    bleAdvertising = BLEDevice::getAdvertising();
    bleAdvertising->setScanResponse(false);
    bleAdvertising->setMinInterval(BLE_ADV_MIN_INTERVAL_UNITS);
    bleAdvertising->setMaxInterval(BLE_ADV_MAX_INTERVAL_UNITS);

    // Read the BLE controller address (string like "AA:BB:CC:DD:EE:FF") and
    // parse it into the `mac` uint64_t so printed and used values match what
    // other devices see on the air.
    std::string bleAddr = BLEDevice::getAddress().toString();
    unsigned int parts[6] = {0};
    if (sscanf(bleAddr.c_str(), "%02x:%02x:%02x:%02x:%02x:%02x",
               &parts[0], &parts[1], &parts[2], &parts[3], &parts[4], &parts[5]) == 6) {
        uint64_t parsed = 0;
        for (int i = 0; i < 6; ++i) {
            parsed = (parsed << 8) | (static_cast<uint64_t>(parts[i]) & 0xFF);
        }
        mac = parsed;
    } else {
        // Fallback to efuse MAC when BLE address parsing fails.
        mac = ESP.getEfuseMac();
    }

    // Now compute node id from the populated `mac`.
    nodeId = computeNodeIdFromMac();

    Serial.print(F("[BLE] Device name: "));
    Serial.println(nodeName);
    Serial.print(F("[BLE] Node ID: "));
    Serial.println(nodeId);
}

void publishBlePacket(
    bool shtReadOk,
    bool vcnlReadOk,
    bool sgpReadOk,
    float temperatureC,
    float humidityRh,
    uint16_t prox,
    int32_t vocIndex
) {
    if (bleAdvertising == nullptr) {
        return;
    }

    SensorBlePayload payload{};
    payload.protocolVersion = 4;
    payload.nodeId = nodeId;
    payload.flags =
        (shtReadOk ? 0x01 : 0x00) |
        (vcnlReadOk ? 0x02 : 0x00) |
        (sgpReadOk ? 0x04 : 0x00);
    payload.temperatureCenti = shtReadOk ? static_cast<int16_t>(temperatureC * 100.0f) : INT16_MIN;
    payload.humidityCenti = shtReadOk ? static_cast<uint16_t>(humidityRh * 100.0f) : 0;
    payload.proximityRaw = vcnlReadOk ? prox : 0;
    payload.vocIndex = sgpReadOk ? static_cast<int16_t>(vocIndex) : -1;
    payload.sequence = packetSequence++;

    uint8_t packet[2 + sizeof(SensorBlePayload)] = {};
    packet[0] = static_cast<uint8_t>(BLE_COMPANY_ID & 0xFF);
    packet[1] = static_cast<uint8_t>((BLE_COMPANY_ID >> 8) & 0xFF);
    memcpy(packet + 2, &payload, sizeof(payload));

    BLEAdvertisementData advData;
    advData.setFlags(0x06);
    advData.setManufacturerData(std::string(reinterpret_cast<char*>(packet), sizeof(packet)));

    bleAdvertising->stop();
    bleAdvertising->setAdvertisementData(advData);
    bleAdvertising->start();
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
    initBleTransport();

    Serial.println(F("\n--- BLE packet update every 1 second ---"));
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
    int32_t vocIndex = -1;
    int32_t noxIndex = -1;

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

    publishBlePacket(
        shtReadOk,
        vcnlReadOk,
        sgpReadOk,
        temperatureC,
        humidityRh,
        prox,
        vocIndex
    );

    Serial.print(F("[BLE TX] node="));
    Serial.print(nodeId);
    Serial.print(F(" mac="));
    Serial.print(mac, HEX);
    Serial.print(F(" seq="));
    Serial.print(static_cast<uint16_t>(packetSequence - 1));
    Serial.print(F(" flags=0x"));
    uint8_t flags =
        (shtReadOk ? 0x01 : 0x00) |
        (vcnlReadOk ? 0x02 : 0x00) |
        (sgpReadOk ? 0x04 : 0x00);
    Serial.print(flags, HEX);
    if (shtReadOk) {
        Serial.print(F(" T="));
        Serial.print(temperatureC, 2);
        Serial.print(F(" RH="));
        Serial.print(humidityRh, 2);
    }
    if (vcnlReadOk) {
        Serial.print(F(" Prox="));
        Serial.print(prox);
        Serial.print(F(" ALS="));
        Serial.print(als);
        Serial.print(F(" White="));
        Serial.print(white);
        Serial.print(F(" Lux="));
        Serial.print(lux, 2);
    }
    if (sgpReadOk) {
        Serial.print(F(" SRAW_VOC="));
        Serial.print(srawVoc);
        Serial.print(F(" SRAW_NOX="));
        Serial.print(srawNox);
    }
    if (sgpReadOk && sgp41ConditioningDone) {
        Serial.print(F(" VOC="));
        Serial.print(vocIndex);
        Serial.print(F(" NOx="));
        Serial.print(noxIndex);
    }
    if (intEvent) {
        Serial.print(F(" INT#="));
        Serial.print(intCountSnapshot);
        if (intFlagsOk) {
            Serial.print(F(" IF=0x"));
            Serial.print(intFlagsRaw, HEX);
        }
    }
    Serial.println();
}