DEFAULT_COMPANY_ID = 0xFFFF
EXPECTED_NODES = 9
STALE_SECONDS = 4.0
EMPTY_CELL_PROXIMITY_THRESHOLD = 20

# How long a metric must remain continuously in a worse zone (green->yellow or
# yellow->red) before the trigger system fires an email alert.
TRIGGER_SUSTAIN_SECONDS = 1

# Absolute floor/ceiling per metric. Used as the outer edge of the "red" zone
# for cells in custom bounds mode, and as the min/max for the bounds editor.
METRIC_LIMITS = {
    "temperature": {"min": -20, "max": 100},
    "humidity": {"min": 0, "max": 100},
    "gas": {"min": 0, "max": 1000},
}

# SmartShelf project's known ESP node IDs.
PROJECT_NODE_IDS = {
    3073: "A1",
    3077: "A2",
    3165: "A3",
    3197: "B1",
    16341: "B2",
    16645: "B3",
    16681: "C1",
    23109: "C2",
    36237: "C3",
}