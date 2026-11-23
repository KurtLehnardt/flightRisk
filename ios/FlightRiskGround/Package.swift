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
    ]
)
