# SmartShelf

SmartShelf monitors food freshness in real time. ESP32 sensor nodes measure conditions around stored items (temperature, humidity, proximity, and VOC/gas) and broadcast the readings over BLE. A Raspberry Pi backend collects those packets, stores and aggregates the data, and serves a live API and dashboard so you can spot spoilage risk early.

Table of Contents
- [Visuals](#visuals)
- [Getting Started](#getting-started)
- [Usage](#usage)
- [License](#license)

Visuals
---
![Dashboard](<images/SmartShelf Dashboard_2026827172626.png>)

Getting Started
---
Prerequisites
- Python 3.8+ for the backend
- PlatformIO (VS Code) or Arduino IDE for the ESP32

Quick setup (Raspberry Pi backend)

Linux / Raspberry Pi:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Windows (development):

```powershell
python -m venv .env
.\.env\Scripts\Activate.ps1
pip install -r requirements.txt
```

ESP32 (firmware)

```bash
# from the ESP32 directory
pio run
pio run -t upload
```

Usage
---
Start the backend (example):

```bash
python RaspberryPi/run.py
```

Example API call (adjust port and routes as implemented):

```bash
curl http://localhost:5000/api/health
```

ESP32: use the serial monitor to view device logs:

```bash
pio device monitor
```

License
---
This project is licensed under the MIT License. See the [LICENSE](LICENSE) file at the repository root for the full terms.
