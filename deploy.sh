#!/bin/sh
set -eu

python -m migrations
./generate_tokens.sh "${TOKEN_USER_COUNT:-5}"
exec python -m main
