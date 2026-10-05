#!/usr/bin/env bash
# Usage: ./generate_tokens.sh [USER_COUNT]   (default 5)
set -euo pipefail

user_count="${1:-${TOKEN_USER_COUNT:-5}}"
if ! [[ "$user_count" =~ ^([2-9]|[1-9][0-9]+)$ ]]; then
	echo "USER_COUNT must be an integer of at least 2" >&2
	exit 1
fi

credentials_dir="${TICKFAST_TOKEN_OUTPUT_DIR:-${HOME}/.tickfast/credentials}"
exec python -m scripts.generate_tokens \
	--users "$user_count" \
	--allow-non-test-database \
	--output-dir "$credentials_dir"
