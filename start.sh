#!/bin/bash
# Starts both the scanner and the web server in parallel
python3 server.py &
python3 scanner.py
