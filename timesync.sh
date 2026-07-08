#!/bin/bash

while true; do
    DATE=$(curl -fsSI https://google.com | awk '/^date:/ {print substr($0,7)}')

    if [ -n "$DATE" ]; then
        if date -u -s "$DATE"; then
            logger "timesync: successfully synchronized time"
            exit 0
        fi
    fi

    logger "timesync: failed to synchronize time, retrying in 30 seconds"
    sleep 30
done
