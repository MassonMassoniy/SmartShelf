
# ESP32 Firmware

ESP32 firmware for SmartShelf — collects sensor data and reports to the Raspberry Pi backend.

Table of Contents
- [Getting Started](#getting-started)
- [Usage](#usage)
- [License](#license)

Getting Started
---
Prerequisites
- PlatformIO (VS Code) or Arduino CLI

Build and upload (PlatformIO)

```bash
cd ESP32
pio run
pio run -t upload
```

Alternative: Arduino sketch

```bash
# open Arduino ESP32/sketch_jun12a/sketch_jun12a.ino in Arduino IDE
```

Usage
---
View serial output (replace port as needed):

```bash
pio device monitor
```

License
---
This project is licensed under the MIT License. See the [LICENSE](LICENSE) file at the repository root for the full terms.

If a `LICENSE` file is not present, create one and add the desired license text (for example, the MIT license). For reference, see: https://opensource.org/licenses/MIT