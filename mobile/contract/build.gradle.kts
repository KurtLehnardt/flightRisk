// The FlightRisk mobile contract: typed models, parsers, derivations and wire
// helpers for the ground station's Socket.IO events and the edge link.
// Pure Kotlin on the JVM, so it compiles and tests with a JDK alone (no Android
// SDK). The JSON fixtures it is tested against live in
// tests/fixtures/mobile_contract at the repository root and are produced by the
// real Python code (docs/mobile-contract.md).
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    // The Kotlin Gradle plugin is already on the build classpath through the
    // root project's org.jetbrains.kotlin.android declaration (Kotlin 2.0.0),
    // so this module applies the JVM plugin from the same artifact without a
    // version of its own.
    id("org.jetbrains.kotlin.jvm")
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    // Only the JSON element API is used (no compiler plugin is needed).
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.1")

    testImplementation("junit:junit:4.13.2")
}

val fixturesDir = layout.projectDirectory.dir("../../tests/fixtures/mobile_contract")

tasks.test {
    useJUnit()
    // The fixtures are a test input: a regenerated fixture reruns the tests.
    inputs.dir(fixturesDir).withPathSensitivity(PathSensitivity.RELATIVE).optional()
    systemProperty("flightrisk.fixturesDir", fixturesDir.asFile.canonicalPath)
    testLogging {
        events("failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}
