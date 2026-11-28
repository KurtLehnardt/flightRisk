# End-to-End Test Suite

This directory contains the E2E test infrastructure, test flows and configuration for FlightRisk.

## Overview

Tool: Maestro (scripted journeys), native crawlers (exploration), monkey (stress)
- One YAML flow per journey runs on Android emulator and iOS simulator
- Identifier-based selection (`id:` matching `testTag` and `accessibilityIdentifier`)
- Synthetic fixtures only: no real images or people
- No real drones, no Wi-Fi connections

## Directory Structure

- `maestro/` - Maestro flow files (`flows/`, `subflows/`)
- `fixtures/` - Synthetic images, video clips, manifest
- `harness/` - Python test infrastructure
- `crawler/` - Denylist and configuration
- `ids/` - Identifier registry (see `ids/README.md`)
- `labels.yml` - GitHub issue labels
- `triage.yml` - Triage rules for filed issues
- `quarantine.yml` - Quarantined journeys

## Journeys

Shared journeys (both platforms): 24
- J01-J07: Onboarding and navigation
- J09, J14-J29: Search, ground link, settings, demo, detection
- Android-only: J08 (camera), J10-J13 (Tello), J30 (lifecycle)
- Phone-only (new): J31-J46 (phone mission mode)

Each journey:
- Starts with `launchApp` with explicit permissions
- Ends with error UI assertions
- Tags: S (smoke, runs on PRs), F (full, nightly)

## Testing Layers

| Layer | What | Where | CI |
|---|---|---|---|
| Unit | Python harness components | `tests/test_e2e_harness_*.py` | pytest |
| Journeys | Maestro flows | `maestro/flows/` | PR (smoke), nightly (full) |
| Crawlers | Native UiAutomator (Android) and XCUITest (iOS) | `src/androidTest`, `src/xcuitest` | Nightly |
| Monkey | Random tap stress | Android only | Nightly |

## Running

Locally (requires emulator or simulator running):

```bash
# Smoke journeys (PR-equivalent)
python e2e/harness/run_e2e.py --platform android --suite smoke --no-file

# Full nightly
python e2e/harness/run_e2e.py --platform android --suite full --no-file

# One journey
python e2e/harness/run_e2e.py --platform android --journey J01

# Exploration
python e2e/harness/run_e2e.py --platform android --explore --seed 1234
```

## CI Workflows

- `.github/workflows/e2e-pr.yml` - Smoke tests on pull requests
- `.github/workflows/e2e-nightly.yml` - Full suite nightly

## Documentation

For detailed design, see `docs/design/` or the `phone-standalone-plan.md` and `e2e-ui-test-plan.md` specification documents.
