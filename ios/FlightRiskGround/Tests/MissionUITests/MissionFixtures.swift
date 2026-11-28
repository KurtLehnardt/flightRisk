import Foundation
import XCTest

/// Locates the golden fixtures in tests/fixtures/mission_core and guards every test transport.
enum MissionFixturesMissionUI {
    /// The repository's mission fixtures directory, found relative to this source file.
    static let directory: URL = {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { url.deleteLastPathComponent() }
        return url.appendingPathComponent("tests/fixtures/mission_core", isDirectory: true)
    }()

    static func data(_ name: String) throws -> Data {
        try Data(contentsOf: directory.appendingPathComponent(name))
    }

    /// Hosts a test transport may use: loopback and the Android emulator's host alias.
    static let allowedHosts: Set<String> = ["127.0.0.1", "::1", "10.0.2.2"]

    static func isAllowed(host: String) -> Bool {
        var h = host.trimmingCharacters(in: .whitespaces)
        if h.hasPrefix("[") && h.hasSuffix("]") { h = String(h.dropFirst().dropLast()) }
        return allowedHosts.contains(h)
    }

    /// Returns the host when it is loopback and stops the test otherwise.
    static func requireLoopback(_ host: String, file: StaticString = #filePath, line: UInt = #line) -> String {
        if !isAllowed(host: host) {
            preconditionFailure("Refusing non-loopback test address: \(host)", file: file, line: line)
        }
        return host
    }
}

final class MissionFixturesScaffoldTests: XCTestCase {
    func testFixturesDirectoryExists() {
        var isDir: ObjCBool = false
        XCTAssertTrue(FileManager.default.fileExists(atPath: MissionFixturesMissionUI.directory.path, isDirectory: &isDir))
        XCTAssertTrue(isDir.boolValue)
    }

    func testGuardAllowsOnlyLoopback() {
        for host in ["127.0.0.1", "::1", "[::1]", "10.0.2.2"] {
            XCTAssertTrue(MissionFixturesMissionUI.isAllowed(host: host), host)
        }
        for host in ["192.168.10.1", "192.168.1.1", "localhost.evil.com", "8.8.8.8", ""] {
            XCTAssertFalse(MissionFixturesMissionUI.isAllowed(host: host), host)
        }
    }
}
