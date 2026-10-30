#!/bin/sh
# Linux / Mac: update Instrumental Studio without opening it.
cd "$(dirname "$0")" && exec python3 update.py "$@"
