#!/bin/sh
# starstack -- run the button. Pass a folder to stack it straight away.
cd "$(dirname "$0")" && exec python3 button.py "$@"
