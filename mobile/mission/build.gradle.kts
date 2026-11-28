// The FlightRisk phone mission core: parameters, planner, preflight checks, state machine,
// drone adapters and the mission log, shared in behaviour with the Swift targets in
// ios/FlightRiskGround. Pure Kotlin on the JVM, so it compiles and tests with a JDK alone
// and runs on x86_64 emulators. The golden fixtures it is tested against live in
// tests/fixtures/mission_core at the repository root and are produced by the real Python code.
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    // Applied without a version, like :contract: the root project already puts the Kotlin
    // Gradle plugin on the build classpath.
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
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.8.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")

    // mavlink-kotlin (Apache-2.0), pinned to 1.2.14: 1.2.15 is built with Kotlin 2.3 metadata, which this build's Kotlin 2.0.0 compiler cannot read.
    // FlightRisk keeps its own transport, so only the typed messages, the coroutines adapter
    // and the UDP connection are pulled in.
    implementation("com.divpundir.mavlink:api:1.2.14")
    implementation("com.divpundir.mavlink:definitions:1.2.14")
    implementation("com.divpundir.mavlink:adapter-coroutines:1.2.14")
    implementation("com.divpundir.mavlink:connection-udp:1.2.14")

    testImplementation("junit:junit:4.13.2")
}

val fixturesDir = layout.projectDirectory.dir("../../tests/fixtures/mission_core")

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
