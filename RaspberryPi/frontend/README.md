# SmartShelf webpage

Flask + Tailwind + HTMX dashboard for ESP32 sensor tiles.
The frontend reads live data from the FastAPI backend (`/nodes/latest`) and refreshes tiles every second.

Quick start (Windows PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python RaspberryPi/run.py

# in another terminal
$env:SMARTSHELF_BACKEND_URL = "http://127.0.0.1:8000"
python RaspberryPi/frontend/main_front.py
```

Open http://127.0.0.1:5000 in your browser.

Backend URL configuration
- Environment variable: `SMARTSHELF_BACKEND_URL`
- Default if not set: `http://127.0.0.1:8000`

Swapping tile variants
- The main grid includes [webpage/templates/index.html](templates/index.html) which currently uses the default `_tile.html` partial.
- To try the semicircle gauge version, edit the include in `index.html` from `_tile.html` to `_tile_gauge.html`.
- To try the horizontal line indicators, change the include to `_tile_line.html`.

Example: in [webpage/templates/index.html](templates/index.html) replace:

```
{% include '_tile.html' %}
```

with:

```
{% include '_tile_gauge.html' %}
```

