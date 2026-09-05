# FlightRisk Mobile (Android)

Android-first mobile client for the FlightRisk AI-powered lost child finder.

## Prerequisites

- **Android Studio** Koala (2024.1.1) or later
- **JDK 17** (bundled with Android Studio or install separately)
- **Android SDK 34** (install via SDK Manager)
- **Kotlin 2.0.0** (managed by Gradle plugin)

## Build

```bash
cd mobile
./gradlew assembleDebug
```

The debug APK is written to `app/build/outputs/apk/debug/`.

## Run Tests

```bash
cd mobile
./gradlew test
```

## Project Structure

```
mobile/
├── build.gradle.kts              # Root build file (plugin versions)
├── settings.gradle.kts           # Project settings
├── gradle.properties             # Gradle/Android/Kotlin config
├── app/
│   ├── build.gradle.kts          # App dependencies & build config
│   ├── proguard-rules.pro        # ProGuard rules for ONNX Runtime
│   └── src/
│       ├── main/
│       │   ├── AndroidManifest.xml
│       │   ├── assets/           # ONNX model files (added later)
│       │   ├── java/com/flightrisk/app/
│       │   │   ├── FlightRiskApp.kt        # Application class
│       │   │   ├── MainActivity.kt          # Compose entry point
│       │   │   └── config/
│       │   │       └── FlightRiskConfig.kt  # Config (port of Python)
│       │   └── res/                         # Resources
│       └── test/                            # JVM unit tests
```

## Key Dependencies

| Library | Version | Purpose |
|---------|---------|---------|
| Jetpack Compose (BOM) | 2024.08.00 | UI framework |
| CameraX | 1.3.4 | Camera access & lifecycle |
| ONNX Runtime | 1.18.0 | On-device ML inference |
| Navigation Compose | 2.7.7 | Screen navigation |
| Play Services Location | 21.3.0 | GPS coordinates |

## Configuration

`FlightRiskConfig` mirrors the Python `AmberConfig` with identical defaults.
Overrides are read from SharedPreferences (keys prefixed `FLIGHTRISK_`).

Three sensitivity presets control alert thresholds:

| Preset | Reid | Scorer | Face |
|--------|------|--------|------|
| MORE_ALERTS | 0.40 | 0.35 | 0.30 |
| BALANCED | 0.55 | 0.45 | 0.45 |
| FEWER_ALERTS | 0.70 | 0.60 | 0.55 |
