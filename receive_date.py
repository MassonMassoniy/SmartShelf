#!/usr/bin/env python3
"""
Simple Flask receiver for date POSTs from ESP32.
Run on the Raspberry Pi and allow port 5000 in the firewall.
"""
from flask import Flask, request
import json
import datetime

app = Flask(__name__)

@app.route('/date', methods=['POST'])
def receive_date():
    data = request.get_json(silent=True)
    if not data:
        return ("bad request", 400)
    # print to console and append to a file with timestamp
    print("Received:", data)
    with open('last_date.txt', 'w') as f:
        f.write(json.dumps(data) + "\n")
    return ('', 204)

if __name__ == '__main__':
    # Listen on all interfaces so ESP can reach it on LAN
    app.run(host='0.0.0.0', port=5000)
