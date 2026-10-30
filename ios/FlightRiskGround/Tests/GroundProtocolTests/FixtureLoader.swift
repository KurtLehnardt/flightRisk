import Foundation
import XCTest
@testable import GroundProtocol

/// Loads the shared mobile contract fixtures from the repository, relative to this file:
/// `<repo>/ios/FlightRiskGround/Tests/GroundProtocolTests/FixtureLoader.swift`.
enum Fixtures {
    static let directory: URL = {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { url.deleteLastPathComponent() }
        return url.appendingPathComponent("tests/fixtures/mobile_contract", isDirectory: true)
    }()

    static let sourcesDirectory: URL = {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<3 { url.deleteLastPathComponent() }
        return url.appendingPathComponent("Sources/GroundProtocol", isDirectory: true)
    }()

    struct Case {
        let file: String
        let name: String
        let event: String?
        let direction: String?
        /// Parsed payload (`NSNull` for a JSON null), or nil when the case has no payload key.
        let payload: Any?
        let payloadData: Data?
        let raw: [String: Any]
    }

    static func fileNames() throws -> [String] {
        try FileManager.default.contentsOfDirectory(atPath: directory.path)
            .filter { $0.hasSuffix(".json") }.map { String($0.dropLast(5)) }.sorted()
    }

    static func load(_ file: String) throws -> (meta: [String: Any], cases: [Case]) {
        let data = try Data(contentsOf: directory.appendingPathComponent(file + ".json"))
        let root = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        let meta = root["_meta"] as? [String: Any] ?? [:]
        let raw = try XCTUnwrap(root["cases"] as? [String: Any])
        let cases: [Case] = try raw.keys.sorted().map { name in
            let c = try XCTUnwrap(raw[name] as? [String: Any])
            let payload = c["payload"]
            var payloadData: Data?
            if let p = payload, !(p is NSNull) {
                payloadData = try JSONSerialization.data(withJSONObject: p, options: [.sortedKeys])
            }
            return Case(file: file, name: name, event: c["event"] as? String,
                        direction: c["direction"] as? String, payload: payload,
                        payloadData: payloadData, raw: c)
        }
        return (meta, cases)
    }

    static func cases(_ file: String) throws -> [Case] { try load(file).cases }

    static func theCase(_ file: String, _ name: String) throws -> Case {
        try XCTUnwrap(try cases(file).first { $0.name == name }, "missing case \(file)/\(name)")
    }

    static func decode<T: Decodable>(_ type: T.Type, _ file: String, _ name: String) throws -> T {
        let c = try theCase(file, name)
        return try JSONDecoder().decode(type, from: XCTUnwrap(c.payloadData))
    }
}
