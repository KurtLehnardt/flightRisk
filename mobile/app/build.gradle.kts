import com.android.build.api.artifact.SingleArtifact
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.google.devtools.ksp")
    id("com.mikepenz.aboutlibraries.plugin")
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

val signingMismatches = mutableListOf<String>()

fun signingValue(propertyName: String, environmentName: String, trim: Boolean = true): String? {
    fun clean(raw: String?): String? {
        if (raw == null) return null
        val value = if (trim) raw.trim() else raw
        return value.takeIf { it.isNotEmpty() }
    }
    val fromFile = clean(keystoreProperties.getProperty(propertyName))
    val fromEnvironment = clean(providers.environmentVariable(environmentName).orNull)
    // Never put a value in the message: it may be a password. The mismatch is recorded here and
    // reported below, only when a release task is going to run, so debug builds, tests and lint
    // are not blocked by a release-signing setup problem.
    if (fromFile != null && fromEnvironment != null && fromFile != fromEnvironment) {
        signingMismatches += "Release signing: $propertyName in keystore.properties differs from $environmentName. " +
            "Set it in only one place, or make the two equal."
    }
    return fromEnvironment ?: fromFile
}

val releaseStoreFile = signingValue("storeFile", "FLIGHTRISK_UPLOAD_STORE_FILE")
val releaseStorePassword = signingValue("storePassword", "FLIGHTRISK_UPLOAD_STORE_PASSWORD", trim = false)
val releaseKeyAlias = signingValue("keyAlias", "FLIGHTRISK_UPLOAD_KEY_ALIAS")
val releaseKeyPassword = signingValue("keyPassword", "FLIGHTRISK_UPLOAD_KEY_PASSWORD", trim = false)
val hasReleaseSigning = listOf(releaseStoreFile, releaseStorePassword, releaseKeyAlias, releaseKeyPassword)
    .all { it != null }

// ---------------------------------------------------------------------------
// On-device models
// ---------------------------------------------------------------------------
// The models are never committed. Before a release build,
// scripts/mobile_models/make_models.py install copies YOLOX-S, the open_clip
// ViT-B-16 embedder, mobile-models.json and model-notices.md into
// src/main/assets (the default) or into the folder given with
// -Pflightrisk.modelAssetsDir=/path (outside the source tree, for example a
// scratch copy), which is then added to the main assets. A release build fails
// when neither holds an installed mobile-models.json naming both models, unless
// -Pflightrisk.allowReleaseWithoutModels=true is given (a build that cannot
// detect or match anyone). See docs/mobile-models.md.
fun optionalProperty(name: String): String? = providers.gradleProperty(name).orNull?.trim()?.takeIf { it.isNotEmpty() }

val modelAssetsDir: File? = optionalProperty("flightrisk.modelAssetsDir")?.let { file(it) }
val allowReleaseWithoutModels = optionalProperty("flightrisk.allowReleaseWithoutModels") == "true"
val requiredModels = listOf("yolox_s.onnx", "clip_vit_b16_laion2b.onnx")

// Unit tests: the golden fixtures (default: the repository's build/mobile_models/golden),
// a folder of installed models to run in the parity tests (-Pflightrisk.modelTestDir=...),
// and a detector to run through the import checks (-Pflightrisk.byoTestModel=...). With
// either of the last two, the desktop ONNX Runtime replaces the Android one in unit tests.
val goldenDir: String = optionalProperty("flightrisk.goldenDir") ?: rootProject.file("../build/mobile_models/golden").path
val modelTestDir: String? = optionalProperty("flightrisk.modelTestDir")
val byoTestModel: String? = optionalProperty("flightrisk.byoTestModel")
val runModelsInUnitTests = modelTestDir != null || byoTestModel != null

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
        // Models installed outside the source tree (see "On-device models" below).
        modelAssetsDir?.let { getByName("main").assets.srcDir(it) }
    }

    // The models are large (the embedder is about 90 MB). Stored uncompressed, they are
    // memory-mapped from the APK instead of being read into the Java heap (OnnxModelLoader).
    androidResources {
        noCompress += "onnx"
    }

    testOptions {
        unitTests.all { test ->
            // The licence-list parse test reads the generated library list.
            test.dependsOn("prepareLibraryDefinitionsRelease")
            test.systemProperty("flightrisk.librariesJson", layout.buildDirectory.file("generated/aboutLibraries/release/res/raw/aboutlibraries.json").get().asFile.path)
            // Golden fixtures of `make_models.py verify` (gitignored; the tests that need them
            // are skipped when they are missing) and, optionally, installed models to run.
            test.systemProperty("flightrisk.goldenDir", goldenDir)
            modelTestDir?.let { test.systemProperty("flightrisk.modelTestDir", it) }
            byoTestModel?.let { test.systemProperty("flightrisk.byoTestModel", it) }
        }
    }
}

// A mismatch between keystore.properties and the environment stays a hard failure for a release
// build (assemble, bundle or any other release task), but only for those.
gradle.taskGraph.whenReady {
    if (signingMismatches.isNotEmpty() && allTasks.any { it.project == project && it.name.contains("Release") }) {
        throw GradleException(signingMismatches.joinToString("\n"))
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

// ---------------------------------------------------------------------------
// Licence guard for store builds
// ---------------------------------------------------------------------------
// Every release APK and bundle is checked right after it is built by
// scripts/mobile_models/licence_guard.py (Python 3.9 or newer, standard library
// only). Only the models scripts/mobile_models/make_models.py installed, listed
// with their SHA-256 in assets/mobile-models.json, may be in it, and no known
// forbidden model (YOLO, Ultralytics, InsightFace, MobileCLIP, OSNet or the
// models the app used to load) under any name. On any finding, or when the
// guard cannot run or finds no APK or bundle to check, the build fails, and a
// rejected APK or bundle is renamed to *.licence-guard-rejected so it cannot be
// uploaded by mistake. Debug builds are not checked. Use another Python
// with -Pflightrisk.python=/path/to/python3 (default: python3 on PATH).
// See docs/mobile-models.md and docs/android-release-signing.md.
val licenceGuardScript: File = rootProject.file("../scripts/mobile_models/licence_guard.py")
// Appended to the name of an APK or bundle the guard rejected.
val licenceGuardRejectedSuffix = ".licence-guard-rejected"
val licenceGuardPython: String = providers.gradleProperty("flightrisk.python")
    .orNull?.trim()?.takeIf { it.isNotEmpty() } ?: "python3"

fun registerLicenceGuard(
    taskName: String,
    what: String,
    target: Provider<out FileSystemLocation>,
): TaskProvider<Exec> = tasks.register<Exec>(taskName) {
    group = "verification"
    description = "Fails the build if the release $what carries a model make_models.py did not install."
    inputs.files(target)
    inputs.file(licenceGuardScript)
    executable = licenceGuardPython
    argumentProviders.add(CommandLineArgumentProvider {
        listOf(licenceGuardScript.absolutePath, target.get().asFile.absolutePath)
    })
    isIgnoreExitValue = true
    doFirst {
        val checked = target.get().asFile
        if (checked.isDirectory) {
            // A file an earlier run rejected is not part of this build, and the guard would check it.
            checked.walkTopDown().filter { it.isFile && it.name.endsWith(licenceGuardRejectedSuffix) }.forEach { it.delete() }
        }
        val found = if (checked.isDirectory) checked.walkTopDown().any { it.isFile && it.extension == "apk" } else checked.isFile
        if (!found) {
            throw GradleException(
                "Licence guard: no release $what to check at $checked (a rejected one is renamed " +
                    "*$licenceGuardRejectedSuffix). Change what was rejected and build again, or run a clean " +
                    "release build (./gradlew clean :app:${if (what == "bundle") "bundle" else "assemble"}Release).",
            )
        }
    }
    doLast {
        val exit = executionResult.get().exitValue
        if (exit != 0) {
            // Rename what was checked, so a rejected APK or bundle cannot be uploaded by mistake.
            val checked = target.get().asFile
            val files = if (checked.isDirectory) {
                checked.walkTopDown().filter { it.isFile && it.extension == "apk" }.toList()
            } else {
                listOf(checked)
            }
            val renamed = files.map { file ->
                val rejected = File(file.path + licenceGuardRejectedSuffix)
                Files.move(file.toPath(), rejected.toPath(), StandardCopyOption.REPLACE_EXISTING)
                rejected.name
            }
            throw GradleException(
                "Licence guard: the release $what at $checked failed the check (exit $exit; the findings are " +
                    "above). Renamed it to ${renamed.joinToString()} so it cannot be uploaded by mistake. Remove " +
                    "every model make_models.py did not install from mobile/app/src/main/assets and build again " +
                    "(docs/mobile-models.md).",
            )
        }
    }
}

val checkReleaseModels = tasks.register("checkReleaseModels") {
    group = "verification"
    description = "Fails a release build that carries no installed models (make_models.py install)."
    val folders = listOfNotNull(file("src/main/assets"), modelAssetsDir)
    doLast {
        if (allowReleaseWithoutModels) {
            logger.warn("WARNING: building a release without checking for the on-device models (flightrisk.allowReleaseWithoutModels).")
            return@doLast
        }
        val ok = folders.any { dir ->
            val manifest = File(dir, "mobile-models.json")
            manifest.isFile && requiredModels.all { name -> File(dir, name).isFile && manifest.readText().contains("\"$name\"") }
        }
        if (!ok) {
            throw GradleException(
                "No installed on-device models in ${folders.joinToString()}: a release build without them cannot " +
                    "detect or match anyone. Run scripts/mobile_models/make_models.py install (docs/mobile-models.md), " +
                    "or point -Pflightrisk.modelAssetsDir at the folder it installed into.",
            )
        }
    }
}

androidComponents {
    onVariants(selector().withBuildType("release")) { variant ->
        val name = variant.name.replaceFirstChar { it.uppercase() }
        tasks.matching { it.name == "merge${name}Assets" }.configureEach { dependsOn(checkReleaseModels) }
        val guardBundle = registerLicenceGuard(
            "licenceGuard${name}Bundle", "bundle", variant.artifacts.get(SingleArtifact.BUNDLE),
        )
        val guardApk = registerLicenceGuard(
            "licenceGuard${name}Apk", "APK", variant.artifacts.get(SingleArtifact.APK),
        )
        tasks.matching { it.name == "bundle$name" || it.name == "assemble$name" }.configureEach { dependsOn(checkLicenceTexts) }
        tasks.matching { it.name == "bundle$name" }.configureEach { finalizedBy(guardBundle) }
        tasks.matching { it.name == "assemble$name" }.configureEach { finalizedBy(guardApk) }
    }
}

// Room writes each schema version to app/schemas (committed). The migration
// tests read these files, and the androidTest assets include them so the
// instrumented migration test can build an old database on a device.
ksp {
    arg("room.schemaLocation", "$projectDir/schemas")
}

// ---------------------------------------------------------------------------
// Library licences
// ---------------------------------------------------------------------------
// AboutLibraries reads every dependency's POM at build time and writes
// aboutlibraries.json (a raw resource) for the Licences screen, with the full
// licence texts. Only the release variant's runtime dependencies are listed.
// Offline and reproducible: the plugin fetches nothing (offlineMode), and the
// licence texts come from the committed files in config/aboutlibraries/licenses
// (the Apache-2.0 SPDX text, libyuv's own notice, and for each MIT or BSD
// library its real licence text with its copyright line, attached per library in
// config/aboutlibraries/libraries). The generic MIT and BSD templates are not
// shipped: their "<year> <owner>" placeholders are not a copyright notice. A new
// MIT or BSD library needs its own licence text with its copyright line, and
// checkLicenceTexts fails the release build until it has one.
// The build fails (strictMode FAIL) when a library has no licence or one
// outside the allowed list, and checkLicenceTexts fails the release build when
// a library has no licence at all or a licence without text (reviewed URL-only
// licences excepted).
aboutLibraries {
    offlineMode = true
    collect {
        configPath = file("config/aboutlibraries")
        includePlatform = false
    }
    library {
        duplicationMode = com.mikepenz.aboutlibraries.plugin.DuplicateMode.MERGE
        duplicationRule = com.mikepenz.aboutlibraries.plugin.DuplicateRule.SIMPLE
    }
    export {
        variant = "release"
    }
    license {
        strictMode = com.mikepenz.aboutlibraries.plugin.StrictMode.FAIL
        allowedLicenses.addAll("Apache-2.0", "MIT", "BSD-3-Clause")
        // Reviewed exceptions, each tied to the one library that carries it.
        allowedLicensesMap.put("Android Software Development Kit License", listOf(
            "com.google.android.gms:play-services-base",
            "com.google.android.gms:play-services-basement",
            "com.google.android.gms:play-services-location",
            "com.google.android.gms:play-services-tasks",
        ))
        // camera-core's POM names libyuv's BSD licence by URL only; the text is committed as "BSD License (libyuv)".
        allowedLicensesMap.put("BSD License", listOf("androidx.camera:camera-core"))
        allowedLicensesMap.put("BSD License (libyuv)", listOf("androidx.camera:camera-core"))
        // MIT and BSD libraries are allowed one by one, each with its own text and copyright line.
        allowedLicensesMap.put("MIT License (ONNX Runtime)", listOf("com.microsoft.onnxruntime:onnxruntime-android"))
        allowedLicensesMap.put("MIT License (Checker Framework annotations)", listOf("org.checkerframework:checker-compat-qual"))
        allowedLicensesMap.put("MIT License (Animal Sniffer)", listOf("org.codehaus.mojo:animal-sniffer-annotations"))
        allowedLicensesMap.put("BSD 3-Clause License (Protocol Buffers)", listOf("com.google.protobuf:protobuf-javalite"))
        allowedLicensesMap.put("GNU General Public License, version 2 (GPL2), with the classpath exception", listOf("org.checkerframework:checker-compat-qual"))
    }
}

// Fails the release build when the generated library list has a library with no
// licence, or a licence with no text. The licences below carry only a URL on
// purpose (reviewed): the Android SDK licence on the Play services libraries,
// and the GPL2 + classpath exception that checker-compat-qual offers beside its
// MIT text.
val urlOnlyLicences = mapOf(
    "ASDKL" to setOf(
        "com.google.android.gms:play-services-base",
        "com.google.android.gms:play-services-basement",
        "com.google.android.gms:play-services-location",
        "com.google.android.gms:play-services-tasks",
    ),
    "da5e524a4235731b9b96281090f5f82d" to setOf("org.checkerframework:checker-compat-qual"),
    // camera-core's POM pointer to libyuv; the libyuv text is attached beside it.
    "44d1a25c593283261968a15c8cc213cd" to setOf("androidx.camera:camera-core"),
)
val genericLicenceIds = setOf("MIT", "BSD-3-Clause")
val checkLicenceTexts = tasks.register("checkLicenceTexts") {
    group = "verification"
    description = "Fails when a library in the generated licence list has no licence or a licence without text."
    val generated = layout.buildDirectory.file("generated/aboutLibraries/release/res/raw/aboutlibraries.json")
    dependsOn("prepareLibraryDefinitionsRelease")
    inputs.file(generated)
    doLast {
        @Suppress("UNCHECKED_CAST")
        val root = groovy.json.JsonSlurper().parse(generated.get().asFile) as Map<String, Any?>
        @Suppress("UNCHECKED_CAST")
        val licences = root["licenses"] as Map<String, Map<String, Any?>>
        @Suppress("UNCHECKED_CAST")
        val libraries = root["libraries"] as List<Map<String, Any?>>
        val problems = mutableListOf<String>()
        for (library in libraries) {
            val id = library["uniqueId"].toString()
            @Suppress("UNCHECKED_CAST")
            val ids = (library["licenses"] as? List<String>).orEmpty()
            if (ids.isEmpty()) problems += "$id: no licence"
            // The plugin merges a library's own licence file with the POM's licence, so a
            // library keeps the POM's generic "MIT" or "BSD-3-Clause" id (no text, no
            // copyright line). That id is accepted only when the library also carries
            // its own text, which is what the per-library licence files provide.
            val hasOwnText = ids.any { other ->
                other !in genericLicenceIds &&
                    other != "Apache-2.0" &&
                    licences[other]?.get("content")?.toString().orEmpty().isNotBlank()
            }
            for (licenceId in ids) {
                val text = licences[licenceId]?.get("content")?.toString().orEmpty()
                if (text.isBlank() && licenceId in genericLicenceIds && hasOwnText) continue
                if (text.isBlank() && id !in (urlOnlyLicences[licenceId] ?: emptySet())) {
                    problems += "$id: licence $licenceId has no text"
                }
            }
        }
        if (problems.isNotEmpty()) {
            throw GradleException(
                "Licence text check failed. Add the text under app/config/aboutlibraries/licenses:\n" +
                    problems.joinToString("\n"),
            )
        }
    }
}

dependencies {
    // Shared ground-station contract (models, decoders, Socket.IO frame codec)
    implementation(project(":contract"))

    // Reads the generated library list for the Licences screen (core only, no UI artifact).
    implementation("com.mikepenz:aboutlibraries-core:12.2.4")
    // The contract's JSON element types appear in its public API
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.8.1")

    // Core Android
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.exifinterface:exifinterface:1.3.7")
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
    // libonnxruntime.so and libonnxruntime4j_jni.so are 16 KB aligned. 1.32.0 (checked
    // with mobile/scripts/check-16kb-alignment.sh) replaces 1.23.2, which follows a
    // model's external-data paths, ../ and absolute ones included; imported models
    // are refused before ONNX Runtime opens them anyway (OnnxModelScanner).
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.32.0")

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
    // A real org.json for unit tests: the Android stub throws, and the licence list parser needs it.
    testImplementation("org.json:json:20240303")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    // A desktop SQLite engine (already in the Gradle cache as a Room compiler
    // dependency) to run the Room migration SQL in FlightRiskMigrationTest.
    testImplementation("org.xerial:sqlite-jdbc:3.41.2.2")
    // The desktop ONNX Runtime (same Java API) runs the installed models in the parity
    // tests; it is only added when they are asked for (see "On-device models" above).
    if (runModelsInUnitTests) testRuntimeOnly("com.microsoft.onnxruntime:onnxruntime:1.32.0")
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

// The Android ONNX Runtime cannot load its native library on a desktop JVM; the
// desktop one above replaces it in unit tests that run models.
if (runModelsInUnitTests) {
    configurations.matching { it.name.endsWith("UnitTestRuntimeClasspath") }.configureEach {
        exclude(group = "com.microsoft.onnxruntime", module = "onnxruntime-android")
    }
}
