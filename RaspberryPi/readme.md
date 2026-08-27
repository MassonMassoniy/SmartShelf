
# Raspberry Pi Backend

Backend services for SmartShelf — ingestion, storage, API, and UI templates.

Table of Contents
- [Getting Started](#getting-started)
- [Usage](#usage)
- [License](#license)

Getting Started
---
Prerequisites
- Python 3.8+
- `pip` and a virtual environment tool

Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Configuration
- Copy `RaspberryPi/backend/config/email_config.example.json` → `email_config.json` and edit as needed.

Usage
---
Start the service:

```bash
python run.py
```

Example API call (adjust host/port to your configuration):

```bash
curl http://localhost:5000/api/health
```

Development & Tests
---
- Run unit tests:

```bash
pytest RaspberryPi/backend/tests
```

- Frontend templates are in `RaspberryPi/frontend/templates`.

License
---
This project is licensed under the MIT License. See the [LICENSE](LICENSE) file at the repository root for the full terms.

If a `LICENSE` file is not present, create one and add the desired license text (for example, the MIT license). For reference, see: https://opensource.org/licenses/MIT