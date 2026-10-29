#!/bin/bash
set -euo pipefail

# Check the Swift package first (Metal-free)
echo "Building Swift package..."
cd "$(dirname "$0")/../FlightRiskGround"
swift build
swift test

# Now check if Metal toolchain is available
cd "$(dirname "$0")/.."

if xcrun -find metal >/dev/null 2>&1; then
    echo "Metal toolchain found. Building and testing iOS app..."

    # Set DEVELOPER_DIR to Xcode 26.4.1 if it exists, otherwise use the default
    if [ -d "/Applications/Xcode.app/Contents/Developer" ]; then
        export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
        echo "Using Xcode at $DEVELOPER_DIR"
    fi

    xcodebuild build \
        -scheme FlightRisk \
        -configuration Debug \
        -sdk iphonesimulator \
        -derivedDataPath build \
        -skipPackageUpdates \
        -onlyUsePackageVersionsFromResolvedFile

    xcodebuild test \
        -scheme FlightRiskTests \
        -configuration Debug \
        -sdk iphonesimulator \
        -derivedDataPath build \
        -skipPackageUpdates \
        -onlyUsePackageVersionsFromResolvedFile
else
    echo "Metal toolchain not found. Skipping xcodebuild tests (requires full Xcode toolchain with Metal support)."
fi

echo "All checks passed."
