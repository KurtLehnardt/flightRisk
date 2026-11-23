# Android release signing and Play App Signing

This note explains how to sign the FlightRisk Android release build for Google Play. It contains no keys or passwords, and none may ever be committed. `.gitignore` excludes `keystore.properties`, `*.jks` and `*.keystore`.

## How the build signs a release

`mobile/app/build.gradle.kts` reads four values:

| Value | `mobile/keystore.properties` key | Environment variable |
|---|---|---|
| Keystore file | `storeFile` | `FLIGHTRISK_UPLOAD_STORE_FILE` |
| Keystore password | `storePassword` | `FLIGHTRISK_UPLOAD_STORE_PASSWORD` |
| Key alias | `keyAlias` | `FLIGHTRISK_UPLOAD_KEY_ALIAS` |
| Key password | `keyPassword` | `FLIGHTRISK_UPLOAD_KEY_PASSWORD` |

- Each value is looked up in the environment first and then in `mobile/keystore.properties`, so a CI job can keep the passwords in its secret store and the rest in the file. The environment wins. If a value is set in both places and the two differ, the build fails (the message names the value, never shows it). Passwords are used exactly as written, with no trimming.
- A relative `storeFile` path is resolved against the `mobile/` directory.
- When all four values are found, `assembleRelease` and `bundleRelease` sign with that key.
- When any value is missing, the release APK and bundle are built unsigned, and Gradle prints a warning that starts with `WARNING: FlightRisk release signing is not configured`. An unsigned bundle cannot be uploaded to Google Play.
- The build never prints the values.

## 1. Create the upload key (once)

Google Play App Signing uses two keys. Google keeps the **app signing key**, which signs what users install. You keep the **upload key**, which signs what you upload. If the upload key is lost or leaked, Google can reset it; the app signing key is not affected.

Create the upload key outside the repository, for example in `~/keys/`:

```sh
keytool -genkeypair -v \
  -keystore ~/keys/flightrisk-upload.jks \
  -storetype PKCS12 \
  -alias flightrisk-upload \
  -keyalg RSA -keysize 4096 \
  -validity 10000
```

`keytool` asks for a password and for the certificate's name fields. With a PKCS12 keystore, the key password is the same as the keystore password.

- Store the keystore file and the password in a password manager, and keep a second copy offline. Without them you cannot publish updates until Google resets the upload key, which takes time.
- Do not copy the keystore into the repository, and do not send it by chat or email.

## 2. Point the build at the key

Create `mobile/keystore.properties` (it is gitignored):

```properties
storeFile=/Users/you/keys/flightrisk-upload.jks
storePassword=<the keystore password>
keyAlias=flightrisk-upload
keyPassword=<the same password>
```

Or set the four environment variables instead, for example in CI. Then build:

```sh
cd mobile
./gradlew bundleRelease
```

The bundle is `mobile/app/build/outputs/bundle/release/app-release.aab`. To check its signature:

```sh
jarsigner -verify -verbose -certs app/build/outputs/bundle/release/app-release.aab | tail -5
```

## 3. Enrol in Play App Signing

Play App Signing is required for new apps that publish Android App Bundles.

1. In Play Console, create the app (Home > Create app).
2. Open Test and release > Setup > App signing (in older Console layouts: Release > Setup > App integrity > App signing).
3. Choose **Use Google-generated key**. This is the recommended choice: Google creates and protects the app signing key.
4. Upload the first signed bundle (`app-release.aab`) to an internal testing track. Play registers the certificate of the key that signed it as the upload key.
5. On the App signing page, note the SHA-256 fingerprints of the app signing key and the upload key. Any Google API that checks the app's certificate needs the app signing key's fingerprint, not the upload key's.

## 4. If the upload key is lost or leaked

In Play Console, open Test and release > Setup > App signing and request an upload key reset. Create a new upload key as in step 1, export its certificate, and attach it to the request:

```sh
keytool -export -rfc \
  -keystore ~/keys/flightrisk-upload-new.jks \
  -alias flightrisk-upload \
  -file upload_certificate.pem
```

Google confirms by email when the new key can be used.

## Before each upload

- Raise `versionCode` (and `versionName` as needed) in `mobile/app/build.gradle.kts`. Play rejects a bundle whose `versionCode` was already used.
- Clear the asset folder of models from older builds. A developer checkout may still hold `yolo11n.onnx`, `scrfd_500m.onnx`, `arcface_mobilefacenet.onnx` or `clip_visual.onnx` in `mobile/app/src/main/assets`; they are gitignored, so `git status` never shows them, and they must never ship. Build releases from a fresh clone, or list the ignored files with `git clean -ndX mobile/app/src/main/assets` and remove them with `git clean -fdX mobile/app/src/main/assets` (this also removes models an earlier `make_models.py install` put there; the next step installs them again). `make_models.py install` replaces only the files it installs and fails while anything else is in the folder.
- Build and install the models: `scripts/mobile_models/make_models.py all --targets android`, in its own virtual environment installed with `pip install --require-hashes -r scripts/mobile_models/requirements.lock` (`docs/mobile-models.md`). It installs `yolox_s.onnx`, `clip_vit_b16_laion2b.onnx`, `mobile-models.json` and `model-notices.md` into `mobile/app/src/main/assets`, and refuses a build whose tools are not the locked set.
- The release build checks itself: `bundleRelease` and `assembleRelease` are followed by `licenceGuardReleaseBundle` and `licenceGuardReleaseApk`, which run `scripts/mobile_models/licence_guard.py` (any Python 3.9 or newer on `PATH` as `python3`, or `-Pflightrisk.python=...`) on the built file. The build fails if the file carries any model `make_models.py` did not install (by name, by SHA-256, or by its bytes, whatever the file is called) or if the guard cannot run. Do not upload a file whose build failed. To check a file by hand: `python3 scripts/mobile_models/licence_guard.py mobile/app/build/outputs/bundle/release/app-release.aab`.
- Build with the hosted privacy policy address: `./gradlew bundleRelease -Pflightrisk.privacyPolicyUrl=https://...` (see `docs/app-store-compliance.md`).
- Check that every 64-bit native library is 16 KB aligned, in both the bundle and the APK: `mobile/scripts/check-16kb-alignment.sh mobile/app/build/outputs/bundle/release/app-release.aab` and the same with the APK path. With no argument the script checks the newest release APK or AAB, and it fails when the file holds no native libraries.
