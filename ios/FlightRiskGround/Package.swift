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
    ]
)
