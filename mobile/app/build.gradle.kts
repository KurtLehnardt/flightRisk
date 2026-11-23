import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.google.devtools.ksp")
}

// ---------------------------------------------------------------------------
// Privacy policy link
// ---------------------------------------------------------------------------
// The privacy notice links to this address. Google Play needs a public, hosted
// page (not a source file on GitHub) before the app is submitted, so set the
// hosted address with -Pflightrisk.privacyPolicyUrl=https://... or in
// ~/.gradle/gradle.properties. The default is the policy in this repository.
val defaultPrivacyPolicyUrl = "https://github.com/KurtLehnardt/flightRisk/blob/main/docs/privacy-policy.md"
val privacyPolicyUrl: String = providers.gradleProperty("flightrisk.privacyPolicyUrl")
    .orNull?.trim()?.takeIf { it.isNotEmpty() } ?: defaultPrivacyPolicyUrl
require(privacyPolicyUrl.startsWith("https://") && privacyPolicyUrl.none { it == '"' || it == '\\' || it.isWhitespace() }) {
    "flightrisk.privacyPolicyUrl must be an https:// address without quotes, backslashes or spaces"
}

// ---------------------------------------------------------------------------
// Release signing
// ---------------------------------------------------------------------------
// The upload key is read from mobile/keystore.properties (gitignored) or from
// environment variables. Each value is looked up in the environment first and
// then in the file, so a CI job can keep the passwords in its secret store and
// the rest in the file. The environment wins, as it does in most build tools.
// If a value is set in both places and the two differ, the build fails instead
// of signing with a key the person did not expect. Passwords are used exactly
// as written (not trimmed). When the four values are not all found, the release
// build is left unsigned and a warning is printed. No value is ever logged.
// See docs/android-release-signing.md.
val keystoreProperties = Properties().apply {
    val file = rootProject.file("keystore.properties")
    if (file.isFile) file.inputStream().use { load(it) }
}

fun signingValue(propertyName: String, environmentName: String, trim: Boolean = true): String? {
    fun clean(raw: String?): String? {
        if (raw == null) return null
        val value = if (trim) raw.trim() else raw
        return value.takeIf { it.isNotEmpty() }
    }
    val fromFile = clean(keystoreProperties.getProperty(propertyName))
    val fromEnvironment = clean(providers.environmentVariable(environmentName).orNull)
    // Never put a value in the message: it may be a password.
    if (fromFile != null && fromEnvironment != null && fromFile != fromEnvironment) {
        throw GradleException(
            "Release signing: $propertyName in keystore.properties differs from $environmentName. " +
                "Set it in only one place, or make the two equal.",
        )
    }
    return fromEnvironment ?: fromFile
}

val releaseStoreFile = signingValue("storeFile", "FLIGHTRISK_UPLOAD_STORE_FILE")
val releaseStorePassword = signingValue("storePassword", "FLIGHTRISK_UPLOAD_STORE_PASSWORD", trim = false)
val releaseKeyAlias = signingValue("keyAlias", "FLIGHTRISK_UPLOAD_KEY_ALIAS")
val releaseKeyPassword = signingValue("keyPassword", "FLIGHTRISK_UPLOAD_KEY_PASSWORD", trim = false)
val hasReleaseSigning = listOf(releaseStoreFile, releaseStorePassword, releaseKeyAlias, releaseKeyPassword)
    .all { it != null }

android {
    namespace = "com.flightrisk.app"
    // Google Play requires new apps and updates to target API level 36
    // (Android 16) from 2026-08-31.
    compileSdk = 36

    defaultConfig {
        applicationId = "com.flightrisk.app"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        buildConfigField("String", "PRIVACY_POLICY_URL", "\"$privacyPolicyUrl\"")
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                // A relative path is resolved against the mobile/ directory.
                storeFile = rootProject.file(releaseStoreFile!!)
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        release {
            if (hasReleaseSigning) {
                signingConfig = signingConfigs.getByName("release")
            }
            isMinifyEnabled = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    sourceSets {
        getByName("androidTest").assets.srcDir("$projectDir/schemas")
    }
}

// A signed release is a build meant for Play. Warn when it still links to the
// policy file on GitHub, which Play does not accept as a privacy policy page.
if (hasReleaseSigning && privacyPolicyUrl == defaultPrivacyPolicyUrl) {
    gradle.taskGraph.whenReady {
        val releaseTasks = allTasks.filter { it.project == project && it.name.contains("Release") }
        if (releaseTasks.isNotEmpty()) {
            logger.warn(
                "WARNING: This signed release still links to the default privacy policy address " +
                    "(the policy file on GitHub). Google Play needs a public, hosted page: set " +
                    "-Pflightrisk.privacyPolicyUrl=https://... (docs/app-store-compliance.md).",
            )
        }
    }
}

// Say clearly when a release build is going to be unsigned. Play only accepts
// an upload signed with the upload key.
if (!hasReleaseSigning) {
    gradle.taskGraph.whenReady {
        val releaseTasks = allTasks.filter { it.project == project && it.name.contains("Release") }
        if (releaseTasks.isNotEmpty()) {
            logger.warn(
                "WARNING: FlightRisk release signing is not configured, so the release APK and bundle " +
                    "will be UNSIGNED and Google Play will not accept them. Create mobile/keystore.properties " +
                    "or set FLIGHTRISK_UPLOAD_STORE_FILE, FLIGHTRISK_UPLOAD_STORE_PASSWORD, " +
                    "FLIGHTRISK_UPLOAD_KEY_ALIAS and FLIGHTRISK_UPLOAD_KEY_PASSWORD " +
                    "(docs/android-release-signing.md).",
            )
        }
    }
}

// Room writes each schema version to app/schemas (committed). The migration
// tests read these files, and the androidTest assets include them so the
// instrumented migration test can build an old database on a device.
ksp {
    arg("room.schemaLocation", "$projectDir/schemas")
}

dependencies {
    // Shared ground-station contract (models, decoders, Socket.IO frame codec)
    implementation(project(":contract"))
    // The contract's JSON element types appear in its public API
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.1")

    // Core Android
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.4")
    implementation("androidx.activity:activity-compose:1.9.1")
    // registerForActivityResult needs Fragment 1.3.0 or newer at run time. CameraX
    // view brings in appcompat 1.1.0 and with it Fragment 1.1.0, which lint
    // (InvalidFragmentVersionForActivityResult) rejects. 1.8.9 is the newest 1.8
    // release; 1.9 needs Lifecycle 2.10 and a newer Kotlin compiler.
    implementation("androidx.fragment:fragment-ktx:1.8.9")

    // Jetpack Compose (BOM)
    implementation(platform("androidx.compose:compose-bom:2024.08.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")

    // Navigation
    implementation("androidx.navigation:navigation-compose:2.7.7")

    // Every native library must be aligned to 16 KB pages for Google Play
    // (Android 15+ devices with 16 KB pages). The versions below are the ones
    // whose arm64-v8a libraries are 16 KB aligned (checked with llvm-readelf).

    // CameraX. 1.4.0 is the first release with 16 KB aligned native libraries.
    implementation("androidx.camera:camera-core:1.4.2")
    implementation("androidx.camera:camera-camera2:1.4.2")
    implementation("androidx.camera:camera-lifecycle:1.4.2")
    implementation("androidx.camera:camera-view:1.4.2")

    // ONNX Runtime Mobile. 1.22.0 is the first release in which both
    // libonnxruntime.so and libonnxruntime4j_jni.so are 16 KB aligned.
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.23.2")

    // LiteRT, the successor of TensorFlow Lite (for the OSNet ReID and InsightFace
    // R18 models). It keeps the org.tensorflow.lite API, and its JNI library is
    // 16 KB aligned; org.tensorflow:tensorflow-lite 2.16.1 is not.
    implementation("com.google.ai.edge.litert:litert:1.4.2")

    // MediaPipe LLM Inference (on-device Gemma / local LLM). 0.10.22 is already
    // 16 KB aligned.
    implementation("com.google.mediapipe:tasks-genai:0.10.22")

    // Location
    implementation("com.google.android.gms:play-services-location:21.3.0")

    // Coroutines
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // Encrypted SharedPreferences (API key storage)
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // Room (session persistence)
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")

    // OkHttp (used for the cloud reasoning request only)
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // Testing
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    // A desktop SQLite engine (already in the Gradle cache as a Room compiler
    // dependency) to run the Room migration SQL in FlightRiskMigrationTest.
    testImplementation("org.xerial:sqlite-jdbc:3.41.2.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    // AndroidJUnitRunner (the testInstrumentationRunner above) and the
    // androidx.test.filters size annotations used by PipelineE2ETest. It was
    // already on the runtime classpath through espresso-core, but not on the
    // compile classpath, so PipelineE2ETest did not compile.
    androidTestImplementation("androidx.test:runner:1.5.0")
    // The BOM must be applied to this configuration too, or ui-test-junit4 has no version.
    androidTestImplementation(platform("androidx.compose:compose-bom:2024.08.00"))
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")

    // Debug
    debugImplementation("androidx.compose.ui:ui-tooling")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
}
