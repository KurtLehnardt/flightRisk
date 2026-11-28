// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "FlightRiskGround",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(
            name: "GroundProtocol",
            targets: ["GroundProtocol"]
        ),
        .library(
            name: "SocketIOMini",
            targets: ["SocketIOMini"]
        ),
        .library(
            name: "PersonVision",
            targets: ["PersonVision"]
        ),
        .library(
            name: "MissionCore",
            targets: ["MissionCore"]
        ),
        .library(
            name: "DroneLinks",
            targets: ["DroneLinks"]
        ),
        .library(
            name: "MissionUI",
            targets: ["MissionUI"]
        ),
    ],
    dependencies: [],
    targets: [
        .target(
            name: "GroundProtocol",
            dependencies: [],
            path: "Sources/GroundProtocol"
        ),
        .testTarget(
            name: "GroundProtocolTests",
            dependencies: ["GroundProtocol"],
            path: "Tests/GroundProtocolTests"
        ),
        .target(
            name: "SocketIOMini",
            dependencies: ["GroundProtocol"],
            path: "Sources/SocketIOMini"
        ),
        .testTarget(
            name: "SocketIOMiniTests",
            dependencies: ["SocketIOMini"],
            path: "Tests/SocketIOMiniTests"
        ),
        // On-device person detection and appearance-embedding preprocessing and postprocessing
        // (docs/mobile-models.md), and the checks for a detector the user imports. Plain Swift,
        // no Core ML, so the contracts are tested here against the golden fixtures.
        .target(
            name: "PersonVision",
            dependencies: [],
            path: "Sources/PersonVision"
        ),
        .testTarget(
            name: "PersonVisionTests",
            dependencies: ["PersonVision"],
            path: "Tests/PersonVisionTests",
            resources: [.copy("Fixtures")]
        ),
    
        // The phone mission core: parameters, planner, preflight checks, state machine and log.
        // No dependencies. Its tests read the shared golden fixtures in tests/fixtures/mission_core.
        .target(
            name: "MissionCore",
            dependencies: [],
            path: "Sources/MissionCore"
        ),
        .testTarget(
            name: "MissionCoreTests",
            dependencies: ["MissionCore"],
            path: "Tests/MissionCoreTests"
        ),
        // Drone adapters, transports, the MAVLink codec, video depacketizing and the runner.
        .target(
            name: "DroneLinks",
            dependencies: ["MissionCore"],
            path: "Sources/DroneLinks"
        ),
        .testTarget(
            name: "DroneLinksTests",
            dependencies: ["DroneLinks", "MissionCore"],
            path: "Tests/DroneLinksTests"
        ),
        // Mission screens. They live in the package so parallel tasks never touch the generated
        // project.pbxproj; the app injects the plan canvas.
        .target(
            name: "MissionUI",
            dependencies: ["MissionCore", "DroneLinks"],
            path: "Sources/MissionUI"
        ),
        .testTarget(
            name: "MissionUITests",
            dependencies: ["MissionUI", "MissionCore"],
            path: "Tests/MissionUITests"
        ),
    ]
)
