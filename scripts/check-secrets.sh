#!/usr/bin/env bash
# Pre-commit hook — blocks commits containing API keys or .env files.
set -e

STAGED=$(git diff --cached --name-only)

# 1. Refuse if .env itself is staged
if echo "$STAGED" | grep -qx '\.env'; then
  echo "ERROR: .env is staged. Remove it: git reset HEAD .env"
  exit 1
fi

# 2. Scan staged file contents for Google Maps API key pattern
if echo "$STAGED" | xargs -r git show --cached -- 2>/dev/null | \
    grep -qE 'AIza[A-Za-z0-9_-]{35}'; then
  echo "ERROR: Possible Google Maps API key found in staged files."
  exit 1
fi

# 3. Scan for API key env vars with literal values
if echo "$STAGED" | xargs -r git show --cached -- 2>/dev/null | \
    grep -qE '(GOOGLE_MAPS_API_KEY|RAILDATA_STATIONS_API_KEY)=[^$\[[:space:]]'; then
  echo "ERROR: API key with a value found in staged files."
  exit 1
fi

exit 0
