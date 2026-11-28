#!/bin/bash
# Create GitHub labels for E2E tests if they don't exist

set -euo pipefail

# Colors as hex without #
declare -A LABELS=(
    ["platform: android"]="3DDC84"
    ["platform: ios"]="555555"
    ["source: e2e"]="0075ca"
    ["e2e: flaky"]="fbca04"
    ["area: release"]="d4c5f9"
    ["needs decision"]="d4c5f9"
)

REPO="${REPO:-KurtLehnardt/flightRisk}"

for label in "${!LABELS[@]}"; do
    color="${LABELS[$label]}"

    # Check if label exists
    if gh label list --repo "$REPO" --limit 1000 | grep -q "^$label"; then
        echo "Label '$label' already exists"
    else
        echo "Creating label '$label' with color $color"
        gh label create "$label" \
            --repo "$REPO" \
            --color "$color" \
            --description "E2E test label"
    fi
done
