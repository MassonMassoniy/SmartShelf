#include <Arduino.h>
#include <Wire.h>
#include <SensirionI2CSgp41.h>
#include <VOCGasIndexAlgorithm.h>
#include <NOxGasIndexAlgorithm.h>
#include <BLEDevice.h>
#include <Preferences.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <string>

namespace {

// XIAO ESP32-S3 wiring and fixed sensor addresses.
constexpr uint8_t PIN_SDA = 5;
constexpr uint8_t PIN_SCL = 6;
constexpr uint8_t PIN_VCNL4040_INT = 9;
constexpr uint8_t ADDR_SHT45 = 0x44;
constexpr uint8_t ADDR_VCNL4040 = 0x60;
constexpr uint8_t ADDR_SGP41 = 0x59;

constexpr uint32_t LOOP_PERIOD_MS = 1000UL;
constexpr uint32_t SERIAL_PERIOD_MS = 10000UL;
constexpr uint32_t SGP41_CONDITIONING_MS = 10UL * 1000UL;
constexpr uint32_t STARTUP_STABILIZATION_MS = 5UL * 60UL * 1000UL;
constexpr uint32_t INSERTION_CONFIRM_MS = 30UL * 1000UL;
constexpr uint32_t REMOVAL_CONFIRM_MS = 60UL * 1000UL;
constexpr uint32_t PRODUCT_SETTLING_MS = 5UL * 60UL * 1000UL;
constexpr uint32_t ENVIRONMENT_RECOVERY_MS = 5UL * 60UL * 1000UL;

constexpr float ACCEPTABLE_TEMPERATURE_MIN_C = -10.0f;
constexpr float ACCEPTABLE_TEMPERATURE_MAX_C = 60.0f;
constexpr float ACCEPTABLE_HUMIDITY_MIN_RH = 0.0f;
constexpr float ENVIRONMENT_FAULT_RH = 90.0f;
constexpr float ENVIRONMENT_RECOVERY_RH = 87.0f;
constexpr float SGP_COMPENSATION_MIN_C = -45.0f;
constexpr float SGP_COMPENSATION_MAX_C = 130.0f;

// The SGP41 datasheet's typical response to a doubling of ethanol under
// controlled conditions. It creates only a dimensionless relative scale;
// SRAW_VOC is not ppm or an absolute TVOC concentration.
constexpr float VOC_TICKS_PER_DOUBLING = 870.0f;
constexpr float MAX_RELATIVE_VOC_LOG2 = 16.0f;
// Conservative initial guard against unrealistically small MAD estimates.
// Tune only from deployment noise data; this is not a food-safety threshold.
constexpr float MIN_NOISE_FLOOR_TICKS = 25.0f;
constexpr float SIX_SIGMA_MULTIPLIER = 6.0f;
constexpr float TEN_SIGMA_MULTIPLIER = 10.0f;

constexpr size_t SRAW_RING_SIZE = 60;
constexpr size_t BASELINE_MINUTE_SAMPLES = 15;

constexpr uint16_t VCNL4040_PS_LOW_THRESHOLD = 1200;
constexpr uint16_t VCNL4040_PS_HIGH_THRESHOLD = 2000;
constexpr uint16_t VCNL4040_PS_CONF12_MAX_RANGE = 0x0B0E;
constexpr uint16_t VCNL4040_PS_CONF3MS_MAX_RANGE = 0x0700;

constexpr uint8_t REG_VCNL4040_ALS_CONF = 0x00;
constexpr uint8_t REG_VCNL4040_PS_CONF12 = 0x03;
constexpr uint8_t REG_VCNL4040_PS_CONF3MS = 0x04;
constexpr uint8_t REG_VCNL4040_PS_THDL = 0x06;
constexpr uint8_t REG_VCNL4040_PS_THDH = 0x07;
constexpr uint8_t REG_VCNL4040_PS_DATA = 0x08;
constexpr uint8_t REG_VCNL4040_ALS_DATA = 0x09;
constexpr uint8_t REG_VCNL4040_WHITE_DATA = 0x0A;
constexpr uint8_t REG_VCNL4040_INT_FLAG = 0x0B;
constexpr uint8_t REG_VCNL4040_ID = 0x0C;
constexpr uint8_t CMD_SHT45_MEASURE_HIGH_PREC = 0xFD;

constexpr uint8_t BLE_PROTOCOL_VERSION = 6;
constexpr uint16_t BLE_COMPANY_ID = 0xFFFF;
constexpr uint16_t BLE_ADV_MIN_INTERVAL_UNITS = 160;
constexpr uint16_t BLE_ADV_MAX_INTERVAL_UNITS = 240;

constexpr uint8_t FLAG_SHT_VALID = 1U << 0;
constexpr uint8_t FLAG_PROXIMITY_VALID = 1U << 1;
constexpr uint8_t FLAG_SGP41_VALID = 1U << 2;
constexpr uint8_t FLAG_BASELINE_VALID = 1U << 3;
constexpr uint8_t FLAG_ENVIRONMENT_FAULT = 1U << 4;
constexpr uint8_t FLAG_SIX_SIGMA_EVIDENCE = 1U << 5;
constexpr uint8_t FLAG_TEN_SIGMA_EVIDENCE = 1U << 6;

constexpr uint8_t NVS_FORMAT_VERSION = 1;
constexpr char NVS_NAMESPACE[] = "smartshelf";

enum class SmartShelfState : uint8_t {
    CONDITIONING = 0,
    STABILIZING = 1,
    EMPTY = 2,
    PRODUCT_SETTLING = 3,
    BASELINING = 4,
    MONITORING = 5,
    VOC_WARNING = 6,  // Reserved: warning policy belongs to the Raspberry Pi.
    VOC_ALERT = 7,    // Reserved: alert policy/latching belongs to the Raspberry Pi.
    ENVIRONMENT_FAULT = 8
};

// Little-endian packed layout: <BHBhHHhBBHHBH.
// v6 extends v5 with phase countdown fields for the dashboard.
// The company ID is the preceding two manufacturer-data bytes and is not
// part of this payload.
struct __attribute__((packed)) SensorBlePayloadV6 {
    uint8_t protocolVersion;
    uint16_t nodeId;
    uint8_t flags;
    int16_t temperatureCenti;
    uint16_t humidityCenti;
    uint16_t proximityRaw;
    int16_t vocIndex;
    uint8_t vocChangeScore;
    uint8_t state;
    uint16_t sequence;
    uint16_t phaseRemainingSec;
    uint8_t concurrentState;
    uint16_t concurrentRemainingSec;
};
static_assert(sizeof(SensorBlePayloadV6) == 21, "Unexpected BLE payload size");

struct SrawRing {
    uint16_t values[SRAW_RING_SIZE] = {};
    size_t count = 0;
    size_t next = 0;
};

struct Measurement {
    float temperatureC = NAN;
    float humidityRh = NAN;
    uint16_t proximity = 0;
    uint16_t als = 0;
    uint16_t white = 0;
    uint16_t srawVoc = 0;
    uint16_t srawNox = 0;
    int32_t vocIndex = -1;
    int32_t noxIndex = -1;
    bool shtOk = false;
    bool proximityOk = false;
    bool sgpOk = false;
    bool acceptedVocSample = false;
};

SensirionI2CSgp41 sgp41;
VOCGasIndexAlgorithm vocAlgorithm;
NOxGasIndexAlgorithm noxAlgorithm;
Preferences preferences;

bool sht45Available = false;
bool vcnl4040Available = false;
bool sgp41Available = false;
volatile bool vcnlIntPending = false;
volatile uint32_t vcnlIntCount = 0;

SmartShelfState lifecycleState = SmartShelfState::CONDITIONING;
uint32_t stateStartedMs = 0;
uint32_t setupMs = 0;
uint32_t lastLoopMs = 0;
uint32_t lastSerialMs = 0;

bool productPresent = false;
bool insertionTimerActive = false;
bool removalTimerActive = false;
uint32_t insertionStartedMs = 0;
uint32_t removalStartedMs = 0;

// Product settling can run concurrently with startup stabilization when a
// product is inserted during the 5-minute warm-up window.
bool settlingActive = false;
uint32_t settlingStartedMs = 0;

bool environmentFault = false;
bool recoveryTimerActive = false;
uint32_t recoveryStartedMs = 0;

SrawRing srawRing;
uint16_t baselineMinuteMedians[BASELINE_MINUTE_SAMPLES] = {};
size_t baselineMinuteCount = 0;
bool filteredSrawValid = false;
uint16_t filteredSrawVoc = 0;
bool baselineValid = false;
uint16_t baselineSrawVoc = 0;
float baselineMadTicks = 0.0f;
float noiseSigmaTicks = MIN_NOISE_FLOOR_TICKS;
int32_t vocDeltaTicks = 0;
float relativeVocLog2 = 0.0f;
float relativeVocRatio = 1.0f;
uint8_t vocChangeScore = 255;
bool sixSigmaEvidence = false;
bool tenSigmaEvidence = false;

bool savedCycleAvailable = false;
bool savedBaselineValid = false;
uint16_t savedBaselineSrawVoc = 0;
float savedBaselineMadTicks = 0.0f;
float savedNoiseSigmaTicks = MIN_NOISE_FLOOR_TICKS;
SmartShelfState savedCycleState = SmartShelfState::EMPTY;

uint16_t packetSequence = 0;
uint16_t nodeId = 0;
uint64_t mac = 0;
char nodeName[20] = {};
BLEAdvertising* bleAdvertising = nullptr;
uint8_t blePacket[2 + sizeof(SensorBlePayloadV6)] = {};
std::string manufacturerData;

bool lastShtReadOk = true;
bool lastProximityReadOk = true;
bool lastSgpReadOk = true;

void clearSrawRing();
void transitionLifecycle(SmartShelfState next, uint32_t now, bool persist);

bool elapsed(uint32_t now, uint32_t started, uint32_t duration) {
    return static_cast<uint32_t>(now - started) >= duration;
}

uint16_t remainingSeconds(uint32_t now, uint32_t started, uint32_t duration) {
    if (elapsed(now, started, duration)) {
        return 0;
    }
    const uint32_t remainingMs = duration - static_cast<uint32_t>(now - started);
    return static_cast<uint16_t>((remainingMs + 999UL) / 1000UL);
}

bool settlingComplete(uint32_t now) {
    return settlingActive && elapsed(now, settlingStartedMs, PRODUCT_SETTLING_MS);
}

void startProductSettling(uint32_t now) {
    settlingActive = true;
    settlingStartedMs = now;
    baselineValid = false;
    baselineMinuteCount = 0;
    filteredSrawValid = false;
    clearSrawRing();
    Serial.println(F("[SETTLE] Product settling started (5 min)"));
}

void clearSettling() {
    settlingActive = false;
    settlingStartedMs = 0;
}

uint16_t baseliningRemainingSeconds() {
    if (lifecycleState != SmartShelfState::BASELINING ||
        baselineMinuteCount >= BASELINE_MINUTE_SAMPLES) {
        return 0;
    }
    const uint32_t remainingSamples =
        (BASELINE_MINUTE_SAMPLES - baselineMinuteCount) * SRAW_RING_SIZE -
        srawRing.count;
    return static_cast<uint16_t>(remainingSamples);
}

float clampFloat(float value, float low, float high) {
    return value < low ? low : (value > high ? high : value);
}

const char* stateName(SmartShelfState state) {
    switch (state) {
        case SmartShelfState::CONDITIONING: return "CONDITIONING";
        case SmartShelfState::STABILIZING: return "STABILIZING";
        case SmartShelfState::EMPTY: return "EMPTY";
        case SmartShelfState::PRODUCT_SETTLING: return "PRODUCT_SETTLING";
        case SmartShelfState::BASELINING: return "BASELINING";
        case SmartShelfState::MONITORING: return "MONITORING";
        case SmartShelfState::VOC_WARNING: return "VOC_WARNING_RESERVED";
        case SmartShelfState::VOC_ALERT: return "VOC_ALERT_RESERVED";
        case SmartShelfState::ENVIRONMENT_FAULT: return "ENVIRONMENT_FAULT";
    }
    return "UNKNOWN";
}

SmartShelfState advertisedState() {
    return environmentFault ? SmartShelfState::ENVIRONMENT_FAULT : lifecycleState;
}

void IRAM_ATTR onVcnl4040Interrupt() {
    vcnlIntPending = true;
    ++vcnlIntCount;
}

uint8_t crc8(const uint8_t* data, uint8_t len) {
    uint8_t crc = 0xFF;
    for (uint8_t i = 0; i < len; ++i) {
        crc ^= data[i];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x80) ? static_cast<uint8_t>((crc << 1) ^ 0x31)
                               : static_cast<uint8_t>(crc << 1);
        }
    }
    return crc;
}

bool i2cDevicePresent(uint8_t address) {
    Wire.beginTransmission(address);
    return Wire.endTransmission() == 0;
}

bool writeRegister16(uint8_t address, uint8_t reg, uint16_t value) {
    Wire.beginTransmission(address);
    Wire.write(reg);
    Wire.write(static_cast<uint8_t>(value & 0xFF));
    Wire.write(static_cast<uint8_t>((value >> 8) & 0xFF));
    return Wire.endTransmission() == 0;
}

bool readRegister16(uint8_t address, uint8_t reg, uint16_t& value) {
    Wire.beginTransmission(address);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) {
        return false;
    }
    if (Wire.requestFrom(address, static_cast<uint8_t>(2)) != 2) {
        return false;
    }
    const uint8_t low = Wire.read();
    const uint8_t high = Wire.read();
    value = static_cast<uint16_t>((static_cast<uint16_t>(high) << 8) | low);
    return true;
}

bool readSht45(float& temperatureC, float& humidityRh) {
    Wire.beginTransmission(ADDR_SHT45);
    Wire.write(CMD_SHT45_MEASURE_HIGH_PREC);
    if (Wire.endTransmission() != 0) {
        return false;
    }
    delay(10);
    if (Wire.requestFrom(ADDR_SHT45, static_cast<uint8_t>(6)) != 6) {
        return false;
    }
    uint8_t data[6];
    for (uint8_t i = 0; i < sizeof(data); ++i) {
        data[i] = Wire.read();
    }
    if (crc8(data, 2) != data[2] || crc8(data + 3, 2) != data[5]) {
        return false;
    }
    const uint16_t rawTemperature = static_cast<uint16_t>((data[0] << 8) | data[1]);
    const uint16_t rawHumidity = static_cast<uint16_t>((data[3] << 8) | data[4]);
    temperatureC = -45.0f + 175.0f * static_cast<float>(rawTemperature) / 65535.0f;
    humidityRh = -6.0f + 125.0f * static_cast<float>(rawHumidity) / 65535.0f;
    humidityRh = clampFloat(humidityRh, 0.0f, 100.0f);
    return std::isfinite(temperatureC) && std::isfinite(humidityRh);
}

uint16_t encodeHumidityTicks(float humidityRh) {
    const float clamped = clampFloat(humidityRh, 0.0f, 100.0f);
    return static_cast<uint16_t>(lroundf(clamped * 65535.0f / 100.0f));
}

uint16_t encodeTemperatureTicks(float temperatureC) {
    const float clamped =
        clampFloat(temperatureC, SGP_COMPENSATION_MIN_C, SGP_COMPENSATION_MAX_C);
    return static_cast<uint16_t>(
        lroundf((clamped + 45.0f) * 65535.0f / 175.0f));
}

template <size_t Capacity>
uint16_t medianU16(const uint16_t* values, size_t count) {
    uint16_t sorted[Capacity];
    for (size_t i = 0; i < count; ++i) {
        sorted[i] = values[i];
    }
    std::sort(sorted, sorted + count);
    if ((count & 1U) != 0U) {
        return sorted[count / 2];
    }
    return static_cast<uint16_t>(
        (static_cast<uint32_t>(sorted[count / 2 - 1]) + sorted[count / 2]) / 2U);
}

float medianFloat(float* values, size_t count) {
    std::sort(values, values + count);
    if ((count & 1U) != 0U) {
        return values[count / 2];
    }
    return (values[count / 2 - 1] + values[count / 2]) * 0.5f;
}

void clearSrawRing() {
    srawRing.count = 0;
    srawRing.next = 0;
}

void appendSraw(uint16_t value) {
    srawRing.values[srawRing.next] = value;
    srawRing.next = (srawRing.next + 1U) % SRAW_RING_SIZE;
    if (srawRing.count < SRAW_RING_SIZE) {
        ++srawRing.count;
    }
}

uint16_t ringMedian() {
    return medianU16<SRAW_RING_SIZE>(srawRing.values, srawRing.count);
}

bool isPersistableCycleState(SmartShelfState state) {
    return state == SmartShelfState::EMPTY ||
           state == SmartShelfState::PRODUCT_SETTLING ||
           state == SmartShelfState::BASELINING ||
           state == SmartShelfState::MONITORING;
}

void savePersistentCycle() {
    if (!preferences.begin(NVS_NAMESPACE, false)) {
        Serial.println(F("[NVS] ERROR: open for write failed"));
        return;
    }
    preferences.putUChar("format", NVS_FORMAT_VERSION);
    preferences.putUChar("protocol", BLE_PROTOCOL_VERSION);
    preferences.putBool("baseValid", baselineValid);
    preferences.putUShort("baseSraw", baselineSrawVoc);
    preferences.putFloat("baseMad", baselineMadTicks);
    preferences.putFloat("noise", noiseSigmaTicks);
    preferences.putUChar("cycleState", static_cast<uint8_t>(lifecycleState));
    preferences.end();

    savedCycleAvailable = true;
    savedBaselineValid = baselineValid;
    savedBaselineSrawVoc = baselineSrawVoc;
    savedBaselineMadTicks = baselineMadTicks;
    savedNoiseSigmaTicks = noiseSigmaTicks;
    savedCycleState = lifecycleState;
}

void loadPersistentCycle() {
    if (!preferences.begin(NVS_NAMESPACE, true)) {
        Serial.println(F("[NVS] No stored cycle"));
        return;
    }
    const uint8_t format = preferences.getUChar("format", 0);
    const uint8_t protocol = preferences.getUChar("protocol", 0);
    const uint8_t rawState =
        preferences.getUChar("cycleState", static_cast<uint8_t>(SmartShelfState::EMPTY));
    if (format == NVS_FORMAT_VERSION && protocol == BLE_PROTOCOL_VERSION &&
        rawState <= static_cast<uint8_t>(SmartShelfState::MONITORING) &&
        rawState >= static_cast<uint8_t>(SmartShelfState::EMPTY)) {
        savedCycleAvailable = true;
        savedBaselineValid = preferences.getBool("baseValid", false);
        savedBaselineSrawVoc = preferences.getUShort("baseSraw", 0);
        savedBaselineMadTicks = preferences.getFloat("baseMad", 0.0f);
        savedNoiseSigmaTicks =
            preferences.getFloat("noise", MIN_NOISE_FLOOR_TICKS);
        savedCycleState = static_cast<SmartShelfState>(rawState);
        Serial.print(F("[NVS] Stored cycle state="));
        Serial.print(stateName(savedCycleState));
        Serial.print(F(" baseline="));
        Serial.println(savedBaselineValid ? "valid" : "none");
    } else {
        Serial.println(F("[NVS] Stored data absent or incompatible"));
    }
    preferences.end();
}

void transitionLifecycle(SmartShelfState next, uint32_t now, bool persist) {
    if (lifecycleState == next) {
        return;
    }
    Serial.print(F("[STATE] "));
    Serial.print(stateName(lifecycleState));
    Serial.print(F(" -> "));
    Serial.println(stateName(next));
    lifecycleState = next;
    stateStartedMs = now;
    if (next == SmartShelfState::BASELINING) {
        baselineMinuteCount = 0;
        filteredSrawValid = false;
        clearSrawRing();
    }
    if (persist && isPersistableCycleState(next)) {
        savePersistentCycle();
    }
}

void clearProductCycle(uint32_t now) {
    baselineValid = false;
    baselineSrawVoc = 0;
    baselineMadTicks = 0.0f;
    noiseSigmaTicks = MIN_NOISE_FLOOR_TICKS;
    baselineMinuteCount = 0;
    filteredSrawValid = false;
    vocChangeScore = 255;
    sixSigmaEvidence = false;
    tenSigmaEvidence = false;
    clearSettling();
    clearSrawRing();
    transitionLifecycle(SmartShelfState::EMPTY, now, false);
    savePersistentCycle();
    Serial.println(F("[CYCLE] Confirmed removal; frozen baseline cleared"));
}

void abortProductDuringStabilize() {
    baselineValid = false;
    baselineSrawVoc = 0;
    baselineMadTicks = 0.0f;
    noiseSigmaTicks = MIN_NOISE_FLOOR_TICKS;
    baselineMinuteCount = 0;
    filteredSrawValid = false;
    vocChangeScore = 255;
    sixSigmaEvidence = false;
    tenSigmaEvidence = false;
    clearSettling();
    clearSrawRing();
    Serial.println(F("[CYCLE] Product removed during stabilization; settling cleared"));
}

bool initSht45() {
    if (!i2cDevicePresent(ADDR_SHT45)) {
        Serial.println(F("[SHT45] ERROR: not detected at 0x44"));
        return false;
    }
    float temperature = NAN;
    float humidity = NAN;
    if (!readSht45(temperature, humidity)) {
        Serial.println(F("[SHT45] ERROR: first CRC-checked measurement failed"));
        return false;
    }
    Serial.print(F("[SHT45] OK T="));
    Serial.print(temperature, 2);
    Serial.print(F(" RH="));
    Serial.println(humidity, 2);
    return true;
}

bool initVcnl4040() {
    if (!i2cDevicePresent(ADDR_VCNL4040)) {
        Serial.println(F("[VCNL4040] ERROR: not detected at 0x60"));
        return false;
    }
    bool ok = true;
    ok &= writeRegister16(ADDR_VCNL4040, REG_VCNL4040_ALS_CONF, 0x0000);
    ok &= writeRegister16(
        ADDR_VCNL4040, REG_VCNL4040_PS_CONF12, VCNL4040_PS_CONF12_MAX_RANGE);
    ok &= writeRegister16(
        ADDR_VCNL4040, REG_VCNL4040_PS_CONF3MS, VCNL4040_PS_CONF3MS_MAX_RANGE);
    ok &= writeRegister16(
        ADDR_VCNL4040, REG_VCNL4040_PS_THDL, VCNL4040_PS_LOW_THRESHOLD);
    ok &= writeRegister16(
        ADDR_VCNL4040, REG_VCNL4040_PS_THDH, VCNL4040_PS_HIGH_THRESHOLD);
    if (!ok) {
        Serial.println(F("[VCNL4040] ERROR: long-range configuration failed"));
        return false;
    }

    uint16_t id = 0;
    uint16_t config12 = 0;
    uint16_t config3 = 0;
    uint16_t proximity = 0;
    uint16_t als = 0;
    uint16_t white = 0;
    ok = readRegister16(ADDR_VCNL4040, REG_VCNL4040_ID, id) &&
         readRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_CONF12, config12) &&
         readRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_CONF3MS, config3) &&
         readRegister16(ADDR_VCNL4040, REG_VCNL4040_PS_DATA, proximity) &&
         readRegister16(ADDR_VCNL4040, REG_VCNL4040_ALS_DATA, als) &&
         readRegister16(ADDR_VCNL4040, REG_VCNL4040_WHITE_DATA, white);
    if (!ok) {
        Serial.println(F("[VCNL4040] ERROR: diagnostic register read failed"));
        return false;
    }
    Serial.print(F("[VCNL4040] OK ID=0x"));
    Serial.print(id, HEX);
    Serial.print(F(" CONF12=0x"));
    Serial.print(config12, HEX);
    Serial.print(F(" CONF3=0x"));
    Serial.print(config3, HEX);
    Serial.print(F(" prox/ALS/white="));
    Serial.print(proximity);
    Serial.print('/');
    Serial.print(als);
    Serial.print('/');
    Serial.print(white);
    Serial.print(F(" thresholds="));
    Serial.print(VCNL4040_PS_LOW_THRESHOLD);
    Serial.print('/');
    Serial.println(VCNL4040_PS_HIGH_THRESHOLD);
    return true;
}

bool initSgp41() {
    if (!i2cDevicePresent(ADDR_SGP41)) {
        Serial.println(F("[SGP41] ERROR: not detected at 0x59"));
        return false;
    }
    sgp41.begin(Wire);
    uint16_t serial[3] = {};
    uint16_t error = sgp41.getSerialNumber(serial);
    if (error != 0) {
        Serial.print(F("[SGP41] ERROR: serial read 0x"));
        Serial.println(error, HEX);
        return false;
    }
    uint16_t selfTest = 0;
    error = sgp41.executeSelfTest(selfTest);
    if (error != 0 || selfTest != 0xD400) {
        Serial.print(F("[SGP41] ERROR: self-test comm/result=0x"));
        Serial.print(error, HEX);
        Serial.print('/');
        Serial.println(selfTest, HEX);
        return false;
    }
    Serial.print(F("[SGP41] OK SN=0x"));
    for (uint8_t i = 0; i < 3; ++i) {
        if (serial[i] < 0x1000) Serial.print('0');
        Serial.print(serial[i], HEX);
    }
    Serial.println();
    return true;
}

void buildNodeName() {
    const uint64_t efuseMac = ESP.getEfuseMac();
    const uint32_t tail = static_cast<uint32_t>(efuseMac & 0xFFFFFFU);
    snprintf(nodeName, sizeof(nodeName), "SmartShelf-%06lX",
             static_cast<unsigned long>(tail));
}

void initBleTransport() {
    buildNodeName();
    BLEDevice::init(nodeName);
    bleAdvertising = BLEDevice::getAdvertising();
    bleAdvertising->setScanResponse(false);
    bleAdvertising->setMinInterval(BLE_ADV_MIN_INTERVAL_UNITS);
    bleAdvertising->setMaxInterval(BLE_ADV_MAX_INTERVAL_UNITS);

    const std::string address = BLEDevice::getAddress().toString();
    unsigned int parts[6] = {};
    if (sscanf(address.c_str(), "%02x:%02x:%02x:%02x:%02x:%02x",
               &parts[0], &parts[1], &parts[2], &parts[3], &parts[4], &parts[5]) == 6) {
        for (uint8_t i = 0; i < 6; ++i) {
            mac = (mac << 8) | (parts[i] & 0xFFU);
        }
        nodeId = static_cast<uint16_t>((parts[4] << 8) | parts[5]);
    } else {
        mac = ESP.getEfuseMac();
        nodeId = static_cast<uint16_t>(mac & 0xFFFFU);
    }
    manufacturerData.reserve(sizeof(blePacket));
    blePacket[0] = static_cast<uint8_t>(BLE_COMPANY_ID & 0xFF);
    blePacket[1] = static_cast<uint8_t>(BLE_COMPANY_ID >> 8);
    Serial.print(F("[BLE] Advertising-only v6 name="));
    Serial.print(nodeName);
    Serial.print(F(" node="));
    Serial.println(nodeId);
}

uint8_t buildFlags(const Measurement& measurement) {
    uint8_t flags = 0;
    if (measurement.shtOk) flags |= FLAG_SHT_VALID;
    if (measurement.proximityOk) flags |= FLAG_PROXIMITY_VALID;
    if (measurement.sgpOk) flags |= FLAG_SGP41_VALID;
    if (baselineValid) flags |= FLAG_BASELINE_VALID;
    if (environmentFault) flags |= FLAG_ENVIRONMENT_FAULT;
    if (sixSigmaEvidence) flags |= FLAG_SIX_SIGMA_EVIDENCE;
    if (tenSigmaEvidence) flags |= FLAG_TEN_SIGMA_EVIDENCE;
    return flags;
}

void publishBlePacket(const Measurement& measurement) {
    if (bleAdvertising == nullptr) {
        return;
    }
    const uint32_t now = millis();
    SensorBlePayloadV6 payload{};
    payload.protocolVersion = BLE_PROTOCOL_VERSION;
    payload.nodeId = nodeId;
    payload.flags = buildFlags(measurement);
    payload.temperatureCenti = measurement.shtOk
        ? static_cast<int16_t>(lroundf(clampFloat(
              measurement.temperatureC * 100.0f, -32767.0f, 32767.0f)))
        : INT16_MIN;
    payload.humidityCenti = measurement.shtOk
        ? static_cast<uint16_t>(lroundf(
              clampFloat(measurement.humidityRh * 100.0f, 0.0f, 10000.0f)))
        : 0;
    payload.proximityRaw =
        measurement.proximityOk ? measurement.proximity : 0;
    payload.vocIndex = measurement.sgpOk
        ? static_cast<int16_t>(std::max<int32_t>(
              INT16_MIN, std::min<int32_t>(INT16_MAX, measurement.vocIndex)))
        : -1;
    const bool scoreAvailable =
        measurement.acceptedVocSample && baselineValid && filteredSrawValid &&
        lifecycleState == SmartShelfState::MONITORING && !environmentFault;
    payload.vocChangeScore = scoreAvailable ? vocChangeScore : 255;
    payload.state = static_cast<uint8_t>(advertisedState());
    payload.sequence = packetSequence++;
    payload.phaseRemainingSec = 0;
    payload.concurrentState = 0;
    payload.concurrentRemainingSec = 0;

    if (!environmentFault) {
        switch (lifecycleState) {
            case SmartShelfState::CONDITIONING:
                payload.phaseRemainingSec =
                    remainingSeconds(now, setupMs, SGP41_CONDITIONING_MS);
                break;
            case SmartShelfState::STABILIZING:
                payload.phaseRemainingSec = remainingSeconds(
                    now, stateStartedMs, STARTUP_STABILIZATION_MS);
                if (settlingActive && !settlingComplete(now)) {
                    payload.concurrentState =
                        static_cast<uint8_t>(SmartShelfState::PRODUCT_SETTLING);
                    payload.concurrentRemainingSec = remainingSeconds(
                        now, settlingStartedMs, PRODUCT_SETTLING_MS);
                }
                break;
            case SmartShelfState::PRODUCT_SETTLING:
                payload.phaseRemainingSec = remainingSeconds(
                    now,
                    settlingActive ? settlingStartedMs : stateStartedMs,
                    PRODUCT_SETTLING_MS);
                break;
            case SmartShelfState::BASELINING:
                payload.phaseRemainingSec = baseliningRemainingSeconds();
                break;
            default:
                break;
        }
    }

    memcpy(blePacket + 2, &payload, sizeof(payload));
    manufacturerData.assign(
        reinterpret_cast<const char*>(blePacket), sizeof(blePacket));
    BLEAdvertisementData advertisementData;
    advertisementData.setFlags(0x06);
    advertisementData.setManufacturerData(manufacturerData);
    bleAdvertising->stop();
    bleAdvertising->setAdvertisementData(advertisementData);
    bleAdvertising->start();
}

void updateEnvironmentFault(const Measurement& measurement, uint32_t now) {
    if (measurement.shtOk && measurement.humidityRh >= ENVIRONMENT_FAULT_RH) {
        recoveryTimerActive = false;
        if (!environmentFault) {
            environmentFault = true;
            Serial.print(F("[ENV] FAULT: RH="));
            Serial.print(measurement.humidityRh, 2);
            Serial.println(F("%; VOC baseline/score inputs paused"));
        }
        return;
    }
    if (!environmentFault) {
        return;
    }
    if (measurement.shtOk &&
        measurement.humidityRh < ENVIRONMENT_RECOVERY_RH) {
        if (!recoveryTimerActive) {
            recoveryTimerActive = true;
            recoveryStartedMs = now;
        } else if (elapsed(now, recoveryStartedMs, ENVIRONMENT_RECOVERY_MS)) {
            environmentFault = false;
            recoveryTimerActive = false;
            filteredSrawValid = false;
            clearSrawRing();
            Serial.println(F("[ENV] RECOVERED after 5 continuous minutes below 87% RH"));
        }
    } else {
        recoveryTimerActive = false;
    }
}

enum class PresenceEvent : uint8_t { NONE, INSERTED, REMOVED };

PresenceEvent updatePresence(bool valid, uint16_t proximity, uint32_t now) {
    if (!valid) {
        insertionTimerActive = false;
        removalTimerActive = false;
        return PresenceEvent::NONE;
    }
    if (!productPresent) {
        removalTimerActive = false;
        if (proximity > VCNL4040_PS_HIGH_THRESHOLD) {
            if (!insertionTimerActive) {
                insertionTimerActive = true;
                insertionStartedMs = now;
            } else if (elapsed(now, insertionStartedMs, INSERTION_CONFIRM_MS)) {
                productPresent = true;
                insertionTimerActive = false;
                Serial.println(F("[PRESENCE] Product insertion confirmed"));
                return PresenceEvent::INSERTED;
            }
        } else {
            insertionTimerActive = false;
        }
    } else {
        insertionTimerActive = false;
        if (proximity < VCNL4040_PS_LOW_THRESHOLD) {
            if (!removalTimerActive) {
                removalTimerActive = true;
                removalStartedMs = now;
            } else if (elapsed(now, removalStartedMs, REMOVAL_CONFIRM_MS)) {
                productPresent = false;
                removalTimerActive = false;
                Serial.println(F("[PRESENCE] Product removal confirmed"));
                return PresenceEvent::REMOVED;
            }
        } else {
            removalTimerActive = false;
        }
    }
    return PresenceEvent::NONE;
}

void restoreAfterStabilization(uint32_t now) {
    if (!productPresent) {
        clearSettling();
        clearProductCycle(now);
        return;
    }
    if (savedCycleAvailable && savedBaselineValid) {
        clearSettling();
        baselineValid = true;
        baselineSrawVoc = savedBaselineSrawVoc;
        baselineMadTicks = std::max(0.0f, savedBaselineMadTicks);
        noiseSigmaTicks = std::max(
            MIN_NOISE_FLOOR_TICKS,
            std::isfinite(savedNoiseSigmaTicks) ? savedNoiseSigmaTicks
                                                : MIN_NOISE_FLOOR_TICKS);
        transitionLifecycle(SmartShelfState::MONITORING, now, false);
        Serial.print(F("[NVS] Restored confirmed-present frozen baseline="));
        Serial.print(baselineSrawVoc);
        Serial.print(F(" MAD="));
        Serial.println(baselineMadTicks, 2);
        return;
    }
    if (!settlingActive) {
        // Product present after warm-up without a settle clock (rare); start one.
        startProductSettling(now);
    }
    if (!settlingComplete(now)) {
        transitionLifecycle(SmartShelfState::PRODUCT_SETTLING, now, true);
        return;
    }
    clearSettling();
    transitionLifecycle(SmartShelfState::BASELINING, now, true);
}

void completeBaseline(uint32_t now) {
    baselineSrawVoc =
        medianU16<BASELINE_MINUTE_SAMPLES>(
            baselineMinuteMedians, BASELINE_MINUTE_SAMPLES);
    float deviations[BASELINE_MINUTE_SAMPLES];
    for (size_t i = 0; i < BASELINE_MINUTE_SAMPLES; ++i) {
        deviations[i] = fabsf(
            static_cast<float>(baselineMinuteMedians[i]) - baselineSrawVoc);
    }
    baselineMadTicks = medianFloat(deviations, BASELINE_MINUTE_SAMPLES);
    noiseSigmaTicks =
        std::max(1.4826f * baselineMadTicks, MIN_NOISE_FLOOR_TICKS);
    baselineValid = true;
    filteredSrawValid = false;
    clearSrawRing();
    transitionLifecycle(SmartShelfState::MONITORING, now, false);
    savePersistentCycle();
    Serial.print(F("[BASELINE] Frozen SRAW="));
    Serial.print(baselineSrawVoc);
    Serial.print(F(" MAD="));
    Serial.print(baselineMadTicks, 2);
    Serial.print(F(" sigma="));
    Serial.println(noiseSigmaTicks, 2);
}

void updateFilteringAndBaseline(const Measurement& measurement, uint32_t now) {
    if (!measurement.acceptedVocSample) {
        return;
    }
    if (lifecycleState == SmartShelfState::CONDITIONING ||
        lifecycleState == SmartShelfState::STABILIZING ||
        lifecycleState == SmartShelfState::EMPTY ||
        lifecycleState == SmartShelfState::PRODUCT_SETTLING) {
        return;
    }

    appendSraw(measurement.srawVoc);
    if (srawRing.count < SRAW_RING_SIZE) {
        return;
    }
    filteredSrawVoc = ringMedian();
    filteredSrawValid = true;

    if (lifecycleState == SmartShelfState::BASELINING) {
        baselineMinuteMedians[baselineMinuteCount++] = filteredSrawVoc;
        Serial.print(F("[BASELINE] Minute median "));
        Serial.print(baselineMinuteCount);
        Serial.print('/');
        Serial.print(BASELINE_MINUTE_SAMPLES);
        Serial.print(F("="));
        Serial.println(filteredSrawVoc);
        clearSrawRing();  // Enforces 15 non-overlapping sets of 60 valid samples.
        filteredSrawValid = false;
        if (baselineMinuteCount == BASELINE_MINUTE_SAMPLES) {
            completeBaseline(now);
        }
    }
}

void updateRelativeEvidence(const Measurement& measurement) {
    vocChangeScore = 255;
    sixSigmaEvidence = false;
    tenSigmaEvidence = false;
    vocDeltaTicks = 0;
    relativeVocLog2 = 0.0f;
    relativeVocRatio = 1.0f;
    if (!measurement.acceptedVocSample || !baselineValid || !filteredSrawValid ||
        lifecycleState != SmartShelfState::MONITORING || environmentFault) {
        return;
    }
    vocDeltaTicks =
        static_cast<int32_t>(baselineSrawVoc) - static_cast<int32_t>(filteredSrawVoc);
    relativeVocLog2 = std::max(
        0.0f, static_cast<float>(vocDeltaTicks) / VOC_TICKS_PER_DOUBLING);
    const float exponent =
        clampFloat(relativeVocLog2, 0.0f, MAX_RELATIVE_VOC_LOG2);
    relativeVocRatio = powf(2.0f, exponent);
    const long score = lroundf(50.0f * relativeVocLog2);
    vocChangeScore = static_cast<uint8_t>(std::max(0L, std::min(100L, score)));
    sixSigmaEvidence =
        vocDeltaTicks >= lroundf(SIX_SIGMA_MULTIPLIER * noiseSigmaTicks);
    tenSigmaEvidence =
        vocDeltaTicks >= lroundf(TEN_SIGMA_MULTIPLIER * noiseSigmaTicks);
    // These booleans are magnitude evidence only. Warning/alert thresholds,
    // persistence, and latching are intentionally owned by the Raspberry Pi.
}

void reportReadEdge(const char* sensor, bool current, bool& previous) {
    if (current != previous) {
        Serial.print('[');
        Serial.print(sensor);
        Serial.println(current ? F("] read recovered") : F("] ERROR: read failed"));
        previous = current;
    }
}

void printDiagnostics(const Measurement& measurement, uint32_t now) {
    if (!elapsed(now, lastSerialMs, SERIAL_PERIOD_MS)) {
        return;
    }
    lastSerialMs = now;
    Serial.print(F("[STATUS] state="));
    Serial.print(stateName(advertisedState()));
    if (environmentFault) {
        Serial.print(F(" held="));
        Serial.print(stateName(lifecycleState));
    }
    Serial.print(F(" T/RH="));
    if (measurement.shtOk) {
        Serial.print(measurement.temperatureC, 2);
        Serial.print('/');
        Serial.print(measurement.humidityRh, 2);
    } else {
        Serial.print(F("NA/NA"));
    }
    Serial.print(F(" prox/present="));
    if (measurement.proximityOk) Serial.print(measurement.proximity);
    else Serial.print(F("NA"));
    Serial.print('/');
    Serial.print(productPresent ? 'Y' : 'N');
    Serial.print(F(" SRAW(raw/med/base/delta)="));
    if (measurement.sgpOk) Serial.print(measurement.srawVoc);
    else Serial.print(F("NA"));
    Serial.print('/');
    if (filteredSrawValid) Serial.print(filteredSrawVoc);
    else Serial.print(F("NA"));
    Serial.print('/');
    if (baselineValid) Serial.print(baselineSrawVoc);
    else Serial.print(F("NA"));
    Serial.print('/');
    if (vocChangeScore != 255) Serial.print(vocDeltaTicks);
    else Serial.print(F("NA"));
    Serial.print(F(" score="));
    if (vocChangeScore != 255) Serial.print(vocChangeScore);
    else Serial.print(F("NA"));
    Serial.print(F(" sigma="));
    if (baselineValid) Serial.print(noiseSigmaTicks, 1);
    else Serial.print(F("NA"));
    Serial.print(F(" e6/e10="));
    Serial.print(sixSigmaEvidence ? 'Y' : 'N');
    Serial.print('/');
    Serial.print(tenSigmaEvidence ? 'Y' : 'N');
    Serial.print(F(" rel(log2/ratio)="));
    Serial.print(relativeVocLog2, 3);
    Serial.print('/');
    Serial.print(relativeVocRatio, 2);
    Serial.print(F(" VOC/NOx="));
    Serial.print(measurement.vocIndex);
    Serial.print('/');
    Serial.print(measurement.noxIndex);
    Serial.print(F(" SRAW_NOx="));
    Serial.print(measurement.srawNox);
    Serial.print(F(" env="));
    Serial.print(environmentFault ? 'Y' : 'N');
    if (lifecycleState == SmartShelfState::STABILIZING ||
        lifecycleState == SmartShelfState::PRODUCT_SETTLING ||
        lifecycleState == SmartShelfState::BASELINING ||
        lifecycleState == SmartShelfState::CONDITIONING) {
        Serial.print(F(" rem="));
        if (lifecycleState == SmartShelfState::CONDITIONING) {
            Serial.print(remainingSeconds(now, setupMs, SGP41_CONDITIONING_MS));
        } else if (lifecycleState == SmartShelfState::STABILIZING) {
            Serial.print(remainingSeconds(now, stateStartedMs, STARTUP_STABILIZATION_MS));
            if (settlingActive) {
                Serial.print(F(" settle="));
                Serial.print(remainingSeconds(now, settlingStartedMs, PRODUCT_SETTLING_MS));
            }
        } else if (lifecycleState == SmartShelfState::PRODUCT_SETTLING) {
            Serial.print(remainingSeconds(
                now,
                settlingActive ? settlingStartedMs : stateStartedMs,
                PRODUCT_SETTLING_MS));
        } else {
            Serial.print(baseliningRemainingSeconds());
        }
        Serial.print('s');
    }
    Serial.println();
}

void readMeasurement(Measurement& measurement, uint32_t now) {
    if (sht45Available) {
        measurement.shtOk =
            readSht45(measurement.temperatureC, measurement.humidityRh);
    }
    if (vcnl4040Available) {
        measurement.proximityOk =
            readRegister16(
                ADDR_VCNL4040, REG_VCNL4040_PS_DATA, measurement.proximity) &&
            readRegister16(ADDR_VCNL4040, REG_VCNL4040_ALS_DATA, measurement.als) &&
            readRegister16(
                ADDR_VCNL4040, REG_VCNL4040_WHITE_DATA, measurement.white);
    }

    if (sgp41Available) {
        const float compensationTemperature =
            measurement.shtOk ? measurement.temperatureC : 25.0f;
        const float compensationHumidity =
            measurement.shtOk ? measurement.humidityRh : 50.0f;
        const uint16_t temperatureTicks =
            encodeTemperatureTicks(compensationTemperature);
        const uint16_t humidityTicks =
            encodeHumidityTicks(compensationHumidity);
        uint16_t error = 0;
        if (lifecycleState == SmartShelfState::CONDITIONING &&
            !elapsed(now, setupMs, SGP41_CONDITIONING_MS)) {
            error = sgp41.executeConditioning(
                humidityTicks, temperatureTicks, measurement.srawVoc);
        } else {
            error = sgp41.measureRawSignals(
                humidityTicks, temperatureTicks,
                measurement.srawVoc, measurement.srawNox);
            if (error == 0) {
                measurement.vocIndex = vocAlgorithm.process(measurement.srawVoc);
                measurement.noxIndex = noxAlgorithm.process(measurement.srawNox);
            }
        }
        measurement.sgpOk = error == 0;
        if (error != 0) {
            Serial.print(F("[SGP41] ERROR: command 0x"));
            Serial.println(error, HEX);
        }
    }

    const bool environmentalRangeOk =
        measurement.shtOk &&
        measurement.temperatureC >= ACCEPTABLE_TEMPERATURE_MIN_C &&
        measurement.temperatureC <= ACCEPTABLE_TEMPERATURE_MAX_C &&
        measurement.humidityRh >= ACCEPTABLE_HUMIDITY_MIN_RH &&
        measurement.humidityRh < ENVIRONMENT_FAULT_RH;
    measurement.acceptedVocSample =
        measurement.sgpOk && environmentalRangeOk && !environmentFault &&
        lifecycleState != SmartShelfState::CONDITIONING;
}

}  // namespace

void setup() {
    Serial.begin(115200);
    delay(1500);
    Serial.println(F("\n=== SmartShelf Autonomous VOC Monitor v5 ==="));
    Serial.println(F("Pins SDA=GPIO5 SCL=GPIO6 VCNL_INT=GPIO9"));
    Serial.println(F("Addresses SHT=0x44 VCNL=0x60 SGP41=0x59"));
    Serial.println(F("ESP publishes evidence only; Pi owns warning/alert policy and latch."));
    Serial.println(F("Install the SGP41 away from droplets/condensation; firmware cannot protect it from liquid water."));

    Wire.begin(PIN_SDA, PIN_SCL);
    Wire.setClock(100000);
    Wire.setTimeOut(20);
    loadPersistentCycle();
    sht45Available = initSht45();
    vcnl4040Available = initVcnl4040();
    sgp41Available = initSgp41();

    if (vcnl4040Available) {
        pinMode(PIN_VCNL4040_INT, INPUT_PULLUP);
        attachInterrupt(
            digitalPinToInterrupt(PIN_VCNL4040_INT),
            onVcnl4040Interrupt, FALLING);
        uint16_t staleFlags = 0;
        readRegister16(
            ADDR_VCNL4040, REG_VCNL4040_INT_FLAG, staleFlags);
        Serial.print(F("[VCNL4040] INT enabled; initial flags=0x"));
        Serial.println(staleFlags, HEX);
    }
    initBleTransport();

    setupMs = millis();
    stateStartedMs = setupMs;
    lastLoopMs = setupMs - LOOP_PERIOD_MS;
    lastSerialMs = setupMs - SERIAL_PERIOD_MS;
}

void loop() {
    const uint32_t now = millis();
    if (!elapsed(now, lastLoopMs, LOOP_PERIOD_MS)) {
        return;
    }
    lastLoopMs = now;

    Measurement measurement;
    readMeasurement(measurement, now);
    reportReadEdge("SHT45", measurement.shtOk, lastShtReadOk);
    reportReadEdge(
        "VCNL4040", measurement.proximityOk, lastProximityReadOk);
    reportReadEdge("SGP41", measurement.sgpOk, lastSgpReadOk);

    if (vcnl4040Available) {
        bool interruptEvent = false;
        uint32_t interruptCount = 0;
        noInterrupts();
        interruptEvent = vcnlIntPending;
        vcnlIntPending = false;
        interruptCount = vcnlIntCount;
        interrupts();
        if (interruptEvent) {
            uint16_t flags = 0;
            const bool flagsOk =
                readRegister16(ADDR_VCNL4040, REG_VCNL4040_INT_FLAG, flags);
            Serial.print(F("[VCNL4040] INT#"));
            Serial.print(interruptCount);
            Serial.print(F(" flags="));
            if (flagsOk) Serial.println(flags, HEX);
            else Serial.println(F("READ_ERROR"));
        }
    }

    updateEnvironmentFault(measurement, now);
    const PresenceEvent presenceEvent =
        updatePresence(measurement.proximityOk, measurement.proximity, now);

    if (lifecycleState == SmartShelfState::CONDITIONING &&
        elapsed(now, setupMs, SGP41_CONDITIONING_MS)) {
        transitionLifecycle(SmartShelfState::STABILIZING, now, false);
    } else if (lifecycleState == SmartShelfState::STABILIZING) {
        if (presenceEvent == PresenceEvent::INSERTED) {
            startProductSettling(now);
        } else if (presenceEvent == PresenceEvent::REMOVED) {
            abortProductDuringStabilize();
        }
        if (elapsed(now, stateStartedMs, STARTUP_STABILIZATION_MS)) {
            restoreAfterStabilization(now);
        }
    } else if (lifecycleState != SmartShelfState::CONDITIONING) {
        if (presenceEvent == PresenceEvent::REMOVED) {
            clearProductCycle(now);
        } else if (presenceEvent == PresenceEvent::INSERTED &&
                   lifecycleState == SmartShelfState::EMPTY) {
            startProductSettling(now);
            transitionLifecycle(
                SmartShelfState::PRODUCT_SETTLING, now, true);
        }
        if (lifecycleState == SmartShelfState::PRODUCT_SETTLING &&
            productPresent &&
            elapsed(
                now,
                settlingActive ? settlingStartedMs : stateStartedMs,
                PRODUCT_SETTLING_MS)) {
            clearSettling();
            transitionLifecycle(SmartShelfState::BASELINING, now, true);
        }
    }

    // Gas Index algorithms adapt continuously for diagnostics. The separate
    // product baseline below is frozen after learning and never adapts.
    updateFilteringAndBaseline(measurement, now);
    updateRelativeEvidence(measurement);
    publishBlePacket(measurement);
    printDiagnostics(measurement, now);
}
