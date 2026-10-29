import Foundation
import XCTest
@testable import SocketIOMini

/// R-3: the codec against `socketio_frames.json`, plus hostile input.
final class SocketIOCodecTests: XCTestCase {
    private let codec = SocketIOCodec()

    private static let frameCaseNames: Set<String> = [
        "engineio_open", "socketio_connect_with_auth", "socketio_connect_without_auth",
        "socketio_connect_ack", "socketio_connect_error", "socketio_event_client_to_server",
        "socketio_event_client_to_server_with_payload", "socketio_event_server_to_client",
        "socketio_event_with_ack_id", "socketio_ack", "engineio_ping", "engineio_pong",
        "socketio_disconnect", "engineio_close",
    ]

    // MARK: - Fixtures

    func testFixtureHasEveryFrameCaseTheSpecNames() throws {
        let cases = try Fixtures.cases("socketio_frames.json")
        XCTAssertEqual(Set(cases.keys), Self.frameCaseNames)
    }

    func testEveryFixtureFrameDecodesToItsTypesAndReencodesByteForByte() throws {
        let cases = try Fixtures.cases("socketio_frames.json")
        for (name, fixture) in cases {
            let frame = try XCTUnwrap(fixture["frame"] as? String, name)
            let packet = try codec.decode(frame)
            XCTAssertEqual(Self.engineType(packet), fixture["engineio_type"] as? String, name)
            if case let .message(socketPacket) = packet {
                XCTAssertEqual(Self.socketType(socketPacket.type), fixture["socketio_type"] as? String, name)
            } else {
                XCTAssertTrue(fixture["socketio_type"] is NSNull, name)
            }
            XCTAssertEqual(try codec.encode(packet), frame, "round trip of \(name)")
        }
    }

    func testClientFramesAreEncodedExactlyAsThePythonLibrariesDo() throws {
        XCTAssertEqual(try codec.encodeConnect(apiKey: GroundAPIKey("test-key")), try Fixtures.frame("socketio_connect_with_auth"))
        XCTAssertEqual(try codec.encodeConnect(apiKey: GroundAPIKey("")), try Fixtures.frame("socketio_connect_without_auth"))
        XCTAssertEqual(try codec.encodeEvent(name: "get_fleet_status", payload: nil), try Fixtures.frame("socketio_event_client_to_server"))
        XCTAssertEqual(
            try codec.encodeEvent(name: "get_fleet_status", payload: nil, ackID: 1),
            try Fixtures.frame("socketio_event_with_ack_id")
        )
        struct Land: Encodable {
            let command = "land"
            let drone_id = "drone-1"
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        XCTAssertEqual(
            try codec.encodeEvent(name: "drone_command", payload: try encoder.encode(Land())),
            try Fixtures.frame("socketio_event_client_to_server_with_payload")
        )
        XCTAssertEqual(try codec.encode(.pong("")), try Fixtures.frame("engineio_pong"))
        XCTAssertEqual(try codec.encode(.message(SocketIOPacket(type: .disconnect))), try Fixtures.frame("socketio_disconnect"))
        XCTAssertEqual(try codec.encode(.close), try Fixtures.frame("engineio_close"))
        XCTAssertEqual(try codec.encodeAck(id: 1), try Fixtures.frame("socketio_ack"))
    }

    func testOpenPacketDecodesTheFixtureValues() throws {
        let cases = try Fixtures.cases("socketio_frames.json")
        let decoded = try XCTUnwrap(cases["engineio_open"]?["decoded"] as? [String: Any])
        guard case let .open(open) = try codec.decode(try Fixtures.frame("engineio_open")) else {
            return XCTFail("not an open packet")
        }
        XCTAssertEqual(open.sid, decoded["sid"] as? String)
        XCTAssertEqual(open.pingIntervalMs, decoded["pingInterval"] as? Int)
        XCTAssertEqual(open.pingTimeoutMs, decoded["pingTimeout"] as? Int)
        XCTAssertEqual(open.maxPayload, decoded["maxPayload"] as? Int)
        XCTAssertEqual(open.upgrades, [])
    }

    func testConnectAckAndConnectErrorCarryTheirFixtureData() throws {
        guard case let .message(ack) = try codec.decode(try Fixtures.frame("socketio_connect_ack")) else {
            return XCTFail("not a message")
        }
        XCTAssertEqual(codec.connectSID(ack), "fake-sio-sid-00000001")
        guard case let .message(error) = try codec.decode(try Fixtures.frame("socketio_connect_error")) else {
            return XCTFail("not a message")
        }
        XCTAssertEqual(error.type, .connectError)
        XCTAssertEqual(codec.connectErrorMessage(error), "Connection rejected by server")
        // Older servers send the message as a bare string.
        guard case let .message(legacy) = try codec.decode(#"44"Not authorized""#) else {
            return XCTFail("not a message")
        }
        XCTAssertEqual(codec.connectErrorMessage(legacy), "Not authorized")
    }

    func testEventFramesSplitIntoNameAndTheFixtureArguments() throws {
        let cases = try Fixtures.cases("socketio_frames.json")
        for (name, fixture) in cases where fixture["socketio_type"] as? String == "EVENT" {
            let decoded = try XCTUnwrap(fixture["decoded"] as? [Any], name)
            guard case let .message(packet) = try codec.decode(try XCTUnwrap(fixture["frame"] as? String)) else {
                return XCTFail(name)
            }
            XCTAssertEqual(packet.ackID, fixture["ack_id"] as? Int, name)
            let event = try codec.decodeEvent(packet)
            XCTAssertEqual(event.name, decoded.first as? String, name)
            XCTAssertEqual(event.arguments.count, decoded.count - 1, name)
            for (argument, expected) in zip(event.arguments, decoded.dropFirst()) {
                let object = try JSONSerialization.jsonObject(with: argument, options: [.fragmentsAllowed])
                XCTAssertEqual(object as? NSObject, expected as? NSObject, name)
            }
        }
    }

    func testAckFrameCarriesItsIdAndEmptyList() throws {
        guard case let .message(packet) = try codec.decode(try Fixtures.frame("socketio_ack")) else {
            return XCTFail("not a message")
        }
        XCTAssertEqual(packet.type, .ack)
        XCTAssertEqual(packet.ackID, 1)
        XCTAssertEqual(packet.data, Data("[]".utf8))
    }

    // MARK: - Tolerance

    func testUnknownKeysPassThroughUntouched() throws {
        let cases = try Fixtures.cases("unknown_keys.json")
        var checked = 0
        for (name, fixture) in cases where fixture["direction"] as? String == "server_to_client" {
            let event = try XCTUnwrap(fixture["event"] as? String, name)
            let payload = try XCTUnwrap(fixture["payload"], name)
            let array = try JSONSerialization.data(withJSONObject: [event, payload])
            let frame = "42" + String(decoding: array, as: UTF8.self)
            guard case let .message(packet) = try codec.decode(frame) else { return XCTFail(name) }
            let decoded = try codec.decodeEvent(packet)
            XCTAssertEqual(decoded.name, event, name)
            let raw = try XCTUnwrap(decoded.arguments.first, name)
            let object = try JSONSerialization.jsonObject(with: raw)
            XCTAssertEqual(object as? NSObject, payload as? NSObject, name)
            XCTAssertTrue(String(decoding: raw, as: UTF8.self).contains("x_future_field"), name)
            checked += 1
        }
        XCTAssertGreaterThanOrEqual(checked, 10)
    }

    func testOpenPacketIgnoresUnknownKeysAndMissingMaxPayload() throws {
        let frame = #"0{"sid":"a","pingInterval":100,"pingTimeout":200,"x_future_field":{"nested":[1,2]}}"#
        guard case let .open(open) = try codec.decode(frame) else { return XCTFail("not open") }
        XCTAssertEqual(open.pingIntervalMs, 100)
        XCTAssertNil(open.maxPayload)
    }

    func testStringsWithBracketsQuotesAndUnicodeSurvive() throws {
        let frame = #"42["ünïcode",{"k":"x\"]}[{y","e":"\\"},"tail"]"#
        guard case let .message(packet) = try codec.decode(frame) else { return XCTFail("not a message") }
        let event = try codec.decodeEvent(packet)
        XCTAssertEqual(event.name, "ünïcode")
        XCTAssertEqual(event.arguments.count, 2)
        XCTAssertEqual(String(decoding: event.arguments[0], as: UTF8.self), #"{"k":"x\"]}[{y","e":"\\"}"#)
        XCTAssertEqual(String(decoding: event.arguments[1], as: UTF8.self), #""tail""#)
    }

    func testOtherNamespacesAreParsed() throws {
        guard case let .message(packet) = try codec.decode(#"42/admin,7["x",{}]"#) else { return XCTFail("not a message") }
        XCTAssertEqual(packet.namespace, "/admin")
        XCTAssertEqual(packet.ackID, 7)
        guard case let .message(bare) = try codec.decode("40/admin") else { return XCTFail("not a message") }
        XCTAssertEqual(bare.namespace, "/admin")
        XCTAssertEqual(try codec.encode(.message(packet)), #"42/admin,7["x",{}]"#)
    }

    // MARK: - Hostile input

    func testMalformedFramesFailWithTypedErrors() {
        let frames: [(String, SocketIOCodecError)] = [
            ("", .emptyFrame),
            ("7", .unknownEngineIOType("7")),
            ("x", .unknownEngineIOType("x")),
            ("4", .malformedPacket("empty Socket.IO packet")),
            ("49", .unknownSocketIOType("9")),
            ("4x", .unknownSocketIOType("x")),
            ("42[", .malformedJSON("unclosed bracket")),
            ("42[]", .malformedPacket("event without a name")),
            ("42[1]", .malformedPacket("event name is not a string")),
            (#"42["a",]"#, .malformedJSON("trailing comma")),
            (#"42["a",,1]"#, .malformedJSON("not valid JSON")),
            (#"42["a"] x"#, .malformedJSON("not valid JSON")),
            (#"42{"a":1}"#, .malformedJSON("expected a JSON array")),
            (#"42["a",{]"#, .malformedJSON("unbalanced brackets")),
            (#"42["a",{"k":1 2}]"#, .malformedJSON("not valid JSON")),
            (#"42["a",tru]"#, .malformedJSON("not valid JSON")),
            (#"42["connect"]"#, .invalidEventName("connect is reserved by Socket.IO")),
            (#"42[""]"#, .invalidEventName("empty")),
            ("0", .malformedJSON("not valid JSON")),
            ("0{}", .invalidOpenPacket("missing sid")),
            ("0[]", .invalidOpenPacket("not a JSON object")),
            (#"0{"sid":"a","pingInterval":-1,"pingTimeout":1}"#, .invalidOpenPacket("missing or invalid pingInterval")),
            (#"0{"sid":"a","pingInterval":true,"pingTimeout":1}"#, .invalidOpenPacket("missing or invalid pingInterval")),
            (#"0{"sid":"a","pingInterval":1e300,"pingTimeout":1}"#, .invalidOpenPacket("missing or invalid pingInterval")),
            (#"0{"sid":"a","pingInterval":10,"pingTimeout":"x"}"#, .invalidOpenPacket("missing or invalid pingTimeout")),
            ("45", .malformedPacket("binary packet without an attachment count")),
            ("451", .malformedPacket("binary packet without an attachment count")),
            ("45-[]", .malformedPacket("binary packet without an attachment count")),
            ("40/" + String(repeating: "n", count: 300), .malformedPacket("namespace too long")),
            ("42" + String(repeating: "9", count: 30) + #"["x"]"#, .ackIDTooLarge),
        ]
        for (frame, expected) in frames {
            XCTAssertThrowsError(try decodeFully(frame), "frame \(frame.prefix(40))") { error in
                XCTAssertEqual(error as? SocketIOCodecError, expected, "frame \(frame.prefix(40))")
            }
        }
    }

    func testOversizedFramesAreRefusedBeforeParsing() throws {
        let small = SocketIOCodec(maxFrameBytes: 1024)
        let big = "42[\"a\",\"" + String(repeating: "x", count: 2000) + "\"]"
        XCTAssertThrowsError(try small.decode(big)) { error in
            XCTAssertEqual(error as? SocketIOCodecError, .frameTooLarge(size: big.utf8.count, limit: 1024))
        }
        XCTAssertThrowsError(try small.decode(data: Data(big.utf8))) { error in
            XCTAssertEqual(error as? SocketIOCodecError, .frameTooLarge(size: big.utf8.count, limit: 1024))
        }
        // The default cap refuses a frame just above 8 MiB.
        let huge = String(repeating: "6", count: SocketIOCodec.defaultMaxFrameBytes + 1)
        XCTAssertThrowsError(try codec.decode(huge)) { error in
            guard case .frameTooLarge = error as? SocketIOCodecError else { return XCTFail("\(error)") }
        }
        // Encoding is capped as well.
        XCTAssertThrowsError(try small.encodeEvent(name: "a", payload: Data(("\"" + String(repeating: "x", count: 2000) + "\"").utf8)))
    }

    func testNonUTF8BytesAreRefused() {
        let bytes = Data([0x34, 0x32, 0x5B, 0x22, 0xFF, 0xFE, 0x22, 0x5D])
        XCTAssertThrowsError(try codec.decode(data: bytes)) { error in
            XCTAssertEqual(error as? SocketIOCodecError, .notUTF8)
        }
        XCTAssertThrowsError(try codec.decode(data: Data([0xC3]))) { error in
            XCTAssertEqual(error as? SocketIOCodecError, .notUTF8)
        }
    }

    func testDeeplyNestedFramesFailWithoutRecursing() {
        let depth = 200_000
        let arrays = #"42["x","# + String(repeating: "[", count: depth) + String(repeating: "]", count: depth) + "]"
        XCTAssertThrowsError(try decodeFully(arrays)) { error in
            XCTAssertEqual(error as? SocketIOCodecError, .nestingTooDeep(limit: SocketIOCodec.defaultMaxNestingDepth))
        }
        let objects = #"42["x","# + String(repeating: #"{"a":"#, count: depth) + "1" + String(repeating: "}", count: depth) + "]"
        XCTAssertThrowsError(try decodeFully(objects)) { error in
            XCTAssertEqual(error as? SocketIOCodecError, .nestingTooDeep(limit: SocketIOCodec.defaultMaxNestingDepth))
        }
        let unclosed = "0" + String(repeating: "{", count: 1_000_000)
        XCTAssertThrowsError(try codec.decode(unclosed)) { error in
            XCTAssertEqual(error as? SocketIOCodecError, .nestingTooDeep(limit: SocketIOCodec.defaultMaxNestingDepth))
        }
        // Nesting within the limit is fine.
        let ok = #"42["x","# + String(repeating: "[", count: 30) + String(repeating: "]", count: 30) + "]"
        XCTAssertNoThrow(try decodeFully(ok))
    }

    func testTooManyArgumentsAreRefused() {
        let frame = #"42["a""# + String(repeating: ",1", count: 100) + "]"
        XCTAssertThrowsError(try decodeFully(frame)) { error in
            XCTAssertEqual(error as? SocketIOCodecError, .tooManyArguments(limit: 17))
        }
    }

    func testEveryTruncationOfEveryFixtureFrameIsHandled() throws {
        let cases = try Fixtures.cases("socketio_frames.json")
        var frames = cases.values.compactMap { $0["frame"] as? String }
        frames.append(#"42["frame",{"image":"/9j/","boxes":[{"bbox_norm":[0.1,0.2,0.3,0.4]}],"k":"a\"b"}]"#)
        for frame in frames {
            let bytes = Array(frame.utf8)
            for length in 0...bytes.count {
                _ = try? decodeFully(String(decoding: bytes[0..<length], as: UTF8.self))
                _ = try? codec.decode(data: Data(bytes[0..<length]))
            }
        }
    }

    func testRandomAndMutatedFramesNeverTrap() throws {
        var rng = SeededGenerator(seed: 0x5EED_F12A)
        let alphabet = Array(#"0123456789[]{}",:/\-abtrue null.eE+ "#.utf8) + [0x00, 0x7F, 0xC3, 0xA9, 0xFF]
        for _ in 0..<5000 {
            let length = Int.random(in: 0...64, using: &rng)
            var bytes: [UInt8] = [UInt8(ascii: "0") + UInt8.random(in: 0...6, using: &rng)]
            if Bool.random(using: &rng) { bytes.append(UInt8(ascii: "0") + UInt8.random(in: 0...6, using: &rng)) }
            for _ in 0..<length {
                bytes.append(alphabet.randomElement(using: &rng)!)
            }
            let data = Data(bytes)
            _ = try? codec.decode(data: data)
            _ = try? decodeFully(String(decoding: bytes, as: UTF8.self))
        }
        let seeds = try Fixtures.cases("socketio_frames.json").values.compactMap { $0["frame"] as? String }.sorted()
        for _ in 0..<3000 {
            var bytes = Array(seeds.randomElement(using: &rng)!.utf8)
            for _ in 0..<Int.random(in: 1...4, using: &rng) where !bytes.isEmpty {
                let index = Int.random(in: 0..<bytes.count, using: &rng)
                switch Int.random(in: 0...2, using: &rng) {
                case 0: bytes[index] = alphabet.randomElement(using: &rng)!
                case 1: bytes.remove(at: index)
                default: bytes.insert(alphabet.randomElement(using: &rng)!, at: index)
                }
            }
            _ = try? decodeFully(String(decoding: bytes, as: UTF8.self))
        }
    }

    func testEncodingRefusesBadNamesAndPayloads() {
        XCTAssertThrowsError(try codec.encodeEvent(name: "", payload: nil)) { error in
            XCTAssertEqual(error as? SocketIOCodecError, .invalidEventName("empty"))
        }
        XCTAssertThrowsError(try codec.encodeEvent(name: "disconnect", payload: nil))
        XCTAssertThrowsError(try codec.encodeEvent(name: String(repeating: "a", count: 300), payload: nil))
        XCTAssertThrowsError(try codec.encodeEvent(name: "a", payload: Data("{".utf8)))
        XCTAssertThrowsError(try codec.encodeEvent(name: "a", payload: Data("{} {}".utf8)))
        XCTAssertThrowsError(try codec.encodeEvent(name: "a", payload: Data(#"{"a":1},"injected""#.utf8)))
        XCTAssertThrowsError(try codec.encodeEvent(name: "a", payload: Data([0xFF])))
        let deep = String(repeating: "[", count: 100) + String(repeating: "]", count: 100)
        XCTAssertThrowsError(try codec.encodeEvent(name: "a", payload: Data(deep.utf8)))
        XCTAssertEqual(try codec.encodeEvent(name: "a/b", payload: Data(" {\"x\":1} ".utf8)), #"42["a/b",{"x":1}]"#)
    }

    // MARK: - Helpers

    private func decodeFully(_ frame: String) throws {
        let packet = try codec.decode(frame)
        if case let .message(socketPacket) = packet, socketPacket.type == .event {
            _ = try codec.decodeEvent(socketPacket)
        }
    }

    private static func engineType(_ packet: EngineIOPacket) -> String {
        switch packet {
        case .open: return "OPEN"
        case .close: return "CLOSE"
        case .ping: return "PING"
        case .pong: return "PONG"
        case .message: return "MESSAGE"
        case .upgrade: return "UPGRADE"
        case .noop: return "NOOP"
        }
    }

    private static func socketType(_ type: SocketIOPacketType) -> String {
        switch type {
        case .connect: return "CONNECT"
        case .disconnect: return "DISCONNECT"
        case .event: return "EVENT"
        case .ack: return "ACK"
        case .connectError: return "CONNECT_ERROR"
        case .binaryEvent: return "BINARY_EVENT"
        case .binaryAck: return "BINARY_ACK"
        }
    }
}

/// A small deterministic generator (SplitMix64) so the fuzz cases repeat exactly.
struct SeededGenerator: RandomNumberGenerator {
    private var state: UInt64

    init(seed: UInt64) {
        state = seed
    }

    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }
}
