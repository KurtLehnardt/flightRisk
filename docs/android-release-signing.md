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

- Each value is looked up in `mobile/keystore.properties` first and then in the environment, so a CI job can keep the passwords in its secret store and the rest in the file.
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
- Build with the hosted privacy policy address: `./gradlew bundleRelease -Pflightrisk.privacyPolicyUrl=https://...` (see `docs/app-store-compliance.md`).
- Check that every 64-bit native library is 16 KB aligned: `mobile/scripts/check-16kb-alignment.sh`.
