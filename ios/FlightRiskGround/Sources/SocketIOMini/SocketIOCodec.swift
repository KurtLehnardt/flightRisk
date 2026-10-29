import Foundation

// MARK: - Errors

/// Every way a frame can fail to decode or encode. Decoding hostile input
/// always ends in one of these errors; it never traps.
public enum SocketIOCodecError: Error, Equatable, Sendable, CustomStringConvertible {
    /// The frame is larger than the configured cap.
    case frameTooLarge(size: Int, limit: Int)
    /// The frame is empty.
    case emptyFrame
    /// A binary frame is not valid UTF-8 text.
    case notUTF8
    /// The Engine.IO packet type digit is unknown.
    case unknownEngineIOType(String)
    /// The Socket.IO packet type digit is unknown.
    case unknownSocketIOType(String)
    /// The packet structure is wrong (for example a missing namespace comma).
    case malformedPacket(String)
    /// A JSON part of the frame is not valid JSON or has the wrong shape.
    case malformedJSON(String)
    /// The JSON nests deeper than the configured limit.
    case nestingTooDeep(limit: Int)
    /// The event carries more arguments than the configured limit.
    case tooManyArguments(limit: Int)
    /// The ack id has too many digits.
    case ackIDTooLarge
    /// The Engine.IO open packet lacks a required field or has an invalid value.
    case invalidOpenPacket(String)
    /// The event name is empty, too long, or reserved by Socket.IO.
    case invalidEventName(String)

    public var description: String {
        switch self {
        case let .frameTooLarge(size, limit):
            return "The frame has \(size) bytes, which is more than the limit of \(limit) bytes."
        case .emptyFrame:
            return "The frame is empty."
        case .notUTF8:
            return "The frame is not valid UTF-8 text."
        case let .unknownEngineIOType(type):
            return "Unknown Engine.IO packet type \(type)."
        case let .unknownSocketIOType(type):
            return "Unknown Socket.IO packet type \(type)."
        case let .malformedPacket(detail):
            return "Malformed packet: \(detail)."
        case let .malformedJSON(detail):
            return "Malformed JSON: \(detail)."
        case let .nestingTooDeep(limit):
            return "The JSON nests deeper than \(limit) levels."
        case let .tooManyArguments(limit):
            return "The event has more than \(limit) arguments."
        case .ackIDTooLarge:
            return "The ack id is too large."
        case let .invalidOpenPacket(detail):
            return "Invalid Engine.IO open packet: \(detail)."
        case let .invalidEventName(detail):
            return "Invalid event name: \(detail)."
        }
    }
}

// MARK: - Packets

/// The contents of the Engine.IO open packet (`0{...}`). Unknown keys are ignored.
public struct EngineIOOpen: Equatable, Sendable {
    public var sid: String
    public var upgrades: [String]
    /// Milliseconds between server pings.
    public var pingIntervalMs: Int
    /// Milliseconds the server waits for a pong.
    public var pingTimeoutMs: Int
    /// The largest payload the server accepts, in bytes, when it says so.
    public var maxPayload: Int?
    /// The JSON exactly as received, so that a decoded packet re-encodes byte for byte.
    public var rawJSON: Data

    public init(sid: String, upgrades: [String], pingIntervalMs: Int, pingTimeoutMs: Int, maxPayload: Int?, rawJSON: Data) {
        self.sid = sid
        self.upgrades = upgrades
        self.pingIntervalMs = pingIntervalMs
        self.pingTimeoutMs = pingTimeoutMs
        self.maxPayload = maxPayload
        self.rawJSON = rawJSON
    }
}

/// The Socket.IO v5 packet types.
public enum SocketIOPacketType: Int, Sendable, CaseIterable {
    case connect = 0
    case disconnect = 1
    case event = 2
    case ack = 3
    case connectError = 4
    case binaryEvent = 5
    case binaryAck = 6
}

/// A Socket.IO v5 packet, carried inside an Engine.IO message packet.
public struct SocketIOPacket: Equatable, Sendable {
    public var type: SocketIOPacketType
    /// The namespace; `/` is the default namespace and is not written on the wire.
    public var namespace: String
    /// The ack id, when the packet has one.
    public var ackID: Int?
    /// The number of binary attachments of a binary packet.
    public var attachments: Int?
    /// The raw JSON data part exactly as received, or nil when the packet has none.
    public var data: Data?

    public init(type: SocketIOPacketType, namespace: String = "/", ackID: Int? = nil, attachments: Int? = nil, data: Data? = nil) {
        self.type = type
        self.namespace = namespace
        self.ackID = ackID
        self.attachments = attachments
        self.data = data
    }
}

/// An Engine.IO v4 packet as it travels in one WebSocket text frame.
public enum EngineIOPacket: Equatable, Sendable {
    case open(EngineIOOpen)
    case close
    case ping(String)
    case pong(String)
    case message(SocketIOPacket)
    case upgrade
    case noop
}

/// A decoded Socket.IO event: its name and the raw JSON of each argument.
public struct SocketIOEvent: Equatable, Sendable {
    public var name: String
    /// The raw JSON bytes of each argument after the name, exactly as received.
    public var arguments: [Data]

    public init(name: String, arguments: [Data]) {
        self.name = name
        self.arguments = arguments
    }
}

// MARK: - Codec

/// Encodes and decodes the Engine.IO v4 / Socket.IO v5 text frames the
/// FlightRisk dashboard speaks (`tests/fixtures/mobile_contract/socketio_frames.json`).
///
/// The codec is strict about structure and tolerant about content: unknown
/// keys in JSON objects are ignored, and payloads are kept as raw JSON bytes
/// so that model decoding (the `GroundProtocol` target) sees exactly what the
/// server sent.
public struct SocketIOCodec: Sendable {
    /// The largest frame the codec accepts or produces, in bytes.
    public var maxFrameBytes: Int
    /// The deepest JSON nesting the codec accepts.
    public var maxNestingDepth: Int
    /// The most arguments an event may carry (the dashboard always sends one).
    public var maxEventArguments: Int

    /// Default frame cap: 8 MiB, below the dashboard's own `maxPayload` of 10 MiB.
    public static let defaultMaxFrameBytes = 8 * 1024 * 1024
    /// Default nesting limit; the deepest dashboard payload nests about eight levels.
    public static let defaultMaxNestingDepth = 64
    /// Event names Socket.IO reserves; they can never be emitted.
    public static let reservedEventNames: Set<String> = [
        "connect", "connect_error", "disconnect", "disconnecting", "newListener", "removeListener",
    ]
    static let maxAckIDDigits = 15
    static let maxNamespaceBytes = 256
    static let maxEventNameBytes = 256

    public init(
        maxFrameBytes: Int = SocketIOCodec.defaultMaxFrameBytes,
        maxNestingDepth: Int = SocketIOCodec.defaultMaxNestingDepth,
        maxEventArguments: Int = 16
    ) {
        self.maxFrameBytes = maxFrameBytes
        self.maxNestingDepth = maxNestingDepth
        self.maxEventArguments = maxEventArguments
    }

    // MARK: Decoding

    /// Decodes one WebSocket text frame.
    public func decode(_ text: String) throws -> EngineIOPacket {
        let size = text.utf8.count
        if size > maxFrameBytes {
            throw SocketIOCodecError.frameTooLarge(size: size, limit: maxFrameBytes)
        }
        return try decode(bytes: Array(text.utf8))
    }

    /// Decodes one WebSocket frame given as bytes, which must be UTF-8 text.
    public func decode(data: Data) throws -> EngineIOPacket {
        if data.count > maxFrameBytes {
            throw SocketIOCodecError.frameTooLarge(size: data.count, limit: maxFrameBytes)
        }
        guard String(data: data, encoding: .utf8) != nil else {
            throw SocketIOCodecError.notUTF8
        }
        return try decode(bytes: Array(data))
    }

    private func decode(bytes: [UInt8]) throws -> EngineIOPacket {
        guard let first = bytes.first else {
            throw SocketIOCodecError.emptyFrame
        }
        let rest = bytes[1...]
        switch first {
        case UInt8(ascii: "0"):
            return .open(try decodeOpen(rest))
        case UInt8(ascii: "1"):
            return .close
        case UInt8(ascii: "2"):
            return .ping(String(decoding: rest, as: UTF8.self))
        case UInt8(ascii: "3"):
            return .pong(String(decoding: rest, as: UTF8.self))
        case UInt8(ascii: "4"):
            return .message(try decodeSocketIO(rest))
        case UInt8(ascii: "5"):
            return .upgrade
        case UInt8(ascii: "6"):
            return .noop
        default:
            throw SocketIOCodecError.unknownEngineIOType(Self.printable(first))
        }
    }

    private func decodeOpen(_ bytes: ArraySlice<UInt8>) throws -> EngineIOOpen {
        let object = try jsonObject(bytes)
        guard let dict = object as? [String: Any] else {
            throw SocketIOCodecError.invalidOpenPacket("not a JSON object")
        }
        guard let sid = dict["sid"] as? String, !sid.isEmpty else {
            throw SocketIOCodecError.invalidOpenPacket("missing sid")
        }
        guard let interval = Self.integer(dict["pingInterval"]), interval > 0 else {
            throw SocketIOCodecError.invalidOpenPacket("missing or invalid pingInterval")
        }
        guard let timeout = Self.integer(dict["pingTimeout"]), timeout > 0 else {
            throw SocketIOCodecError.invalidOpenPacket("missing or invalid pingTimeout")
        }
        let upgrades = (dict["upgrades"] as? [Any])?.compactMap { $0 as? String } ?? []
        return EngineIOOpen(
            sid: sid,
            upgrades: upgrades,
            pingIntervalMs: interval,
            pingTimeoutMs: timeout,
            maxPayload: Self.integer(dict["maxPayload"]),
            rawJSON: Data(bytes)
        )
    }

    /// Decodes the Socket.IO packet inside an Engine.IO message (the bytes after the leading `4`).
    public func decodeSocketIO(_ bytes: ArraySlice<UInt8>) throws -> SocketIOPacket {
        guard let first = bytes.first else {
            throw SocketIOCodecError.malformedPacket("empty Socket.IO packet")
        }
        guard first >= UInt8(ascii: "0"), first <= UInt8(ascii: "9"),
              let type = SocketIOPacketType(rawValue: Int(first - UInt8(ascii: "0")))
        else {
            throw SocketIOCodecError.unknownSocketIOType(Self.printable(first))
        }
        var index = bytes.startIndex + 1

        var attachments: Int?
        if type == .binaryEvent || type == .binaryAck {
            var count = 0
            var digits = 0
            while index < bytes.endIndex, let digit = Self.digit(bytes[index]) {
                digits += 1
                if digits > 6 { throw SocketIOCodecError.malformedPacket("attachment count too large") }
                count = count * 10 + digit
                index += 1
            }
            guard digits > 0, index < bytes.endIndex, bytes[index] == UInt8(ascii: "-") else {
                throw SocketIOCodecError.malformedPacket("binary packet without an attachment count")
            }
            index += 1
            attachments = count
        }

        var namespace = "/"
        if index < bytes.endIndex, bytes[index] == UInt8(ascii: "/") {
            let start = index
            while index < bytes.endIndex, bytes[index] != UInt8(ascii: ",") {
                index += 1
                if index - start > Self.maxNamespaceBytes {
                    throw SocketIOCodecError.malformedPacket("namespace too long")
                }
            }
            guard index < bytes.endIndex else {
                // A bare namespace with nothing after it, such as "40/admin".
                namespace = String(decoding: bytes[start..<index], as: UTF8.self)
                return SocketIOPacket(type: type, namespace: namespace, attachments: attachments)
            }
            namespace = String(decoding: bytes[start..<index], as: UTF8.self)
            index += 1 // the comma
        }

        var ackID: Int?
        var digits = 0
        var value = 0
        while index < bytes.endIndex, let digit = Self.digit(bytes[index]) {
            digits += 1
            if digits > Self.maxAckIDDigits { throw SocketIOCodecError.ackIDTooLarge }
            value = value * 10 + digit
            index += 1
        }
        if digits > 0 { ackID = value }

        var data: Data?
        if index < bytes.endIndex {
            let part = bytes[index...]
            try JSONScanner.checkStructure(part, maxDepth: maxNestingDepth)
            _ = try jsonObject(part)
            data = Data(part)
        }
        return SocketIOPacket(type: type, namespace: namespace, ackID: ackID, attachments: attachments, data: data)
    }

    /// Splits an EVENT packet's data into the event name and the raw JSON of each argument.
    public func decodeEvent(_ packet: SocketIOPacket) throws -> SocketIOEvent {
        guard packet.type == .event || packet.type == .ack else {
            throw SocketIOCodecError.malformedPacket("not an event packet")
        }
        guard let data = packet.data else {
            throw SocketIOCodecError.malformedPacket("event without data")
        }
        let bytes = Array(data)
        let ranges = try JSONScanner.splitTopLevelArray(
            bytes[...], maxDepth: maxNestingDepth, maxElements: maxEventArguments + 1
        )
        guard let nameRange = ranges.first else {
            throw SocketIOCodecError.malformedPacket("event without a name")
        }
        guard let name = try jsonObject(bytes[nameRange]) as? String else {
            throw SocketIOCodecError.malformedPacket("event name is not a string")
        }
        try Self.validateEventName(name)
        let arguments = ranges.dropFirst().map { Data(bytes[$0]) }
        return SocketIOEvent(name: name, arguments: arguments)
    }

    /// Reads the `sid` from a CONNECT acknowledgement, or nil when it has none.
    public func connectSID(_ packet: SocketIOPacket) -> String? {
        guard let data = packet.data,
              let dict = try? jsonObject(Array(data)[...]) as? [String: Any]
        else { return nil }
        return dict["sid"] as? String
    }

    /// Reads the message of a CONNECT_ERROR packet (an object with `message`, or a bare string).
    public func connectErrorMessage(_ packet: SocketIOPacket) -> String? {
        guard let data = packet.data, let object = try? jsonObject(Array(data)[...]) else { return nil }
        if let dict = object as? [String: Any] {
            return dict["message"] as? String
        }
        return object as? String
    }

    // MARK: Encoding

    /// Encodes an Engine.IO packet into one text frame.
    public func encode(_ packet: EngineIOPacket) throws -> String {
        switch packet {
        case let .open(open):
            return "0" + String(decoding: open.rawJSON, as: UTF8.self)
        case .close:
            return "1"
        case let .ping(data):
            return "2" + data
        case let .pong(data):
            return "3" + data
        case let .message(socketPacket):
            return "4" + (try encodeSocketIO(socketPacket))
        case .upgrade:
            return "5"
        case .noop:
            return "6"
        }
    }

    /// Encodes a Socket.IO packet (without the Engine.IO `4` prefix).
    public func encodeSocketIO(_ packet: SocketIOPacket) throws -> String {
        var out = String(packet.type.rawValue)
        if packet.type == .binaryEvent || packet.type == .binaryAck {
            out += String(packet.attachments ?? 0) + "-"
        }
        if packet.namespace != "/" {
            out += packet.namespace + ","
        }
        if let ackID = packet.ackID {
            guard ackID >= 0, String(ackID).count <= Self.maxAckIDDigits else {
                throw SocketIOCodecError.ackIDTooLarge
            }
            out += String(ackID)
        }
        if let data = packet.data {
            out += String(decoding: data, as: UTF8.self)
        }
        try checkSize(out)
        return out
    }

    /// The namespace connect frame: `40{"api_key":"..."}`, or `40` when the key is empty.
    func encodeConnect(apiKey: GroundAPIKey) throws -> String {
        guard !apiKey.isEmpty else {
            return "40"
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let json = try encoder.encode(["api_key": apiKey.secret])
        return "40" + String(decoding: json, as: UTF8.self)
    }

    /// An event frame: `42["name",payload]`, or `42<ackID>[...]` with an ack id.
    /// `payload` must be one complete JSON value; nil sends the event with no argument.
    public func encodeEvent(name: String, payload: Data?, ackID: Int? = nil) throws -> String {
        try Self.validateEventName(name)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.withoutEscapingSlashes]
        var array = Data("[".utf8)
        array.append(try encoder.encode(name))
        if let payload {
            let trimmed = Self.trimmed(payload)
            try checkSize(trimmed.count)
            try JSONScanner.checkStructure(Array(trimmed)[...], maxDepth: maxNestingDepth - 1)
            do {
                _ = try JSONSerialization.jsonObject(with: trimmed, options: [.fragmentsAllowed])
            } catch {
                throw SocketIOCodecError.malformedJSON("the payload is not valid JSON")
            }
            array.append(UInt8(ascii: ","))
            array.append(trimmed)
        }
        array.append(UInt8(ascii: "]"))
        return "4" + (try encodeSocketIO(SocketIOPacket(type: .event, ackID: ackID, data: array)))
    }

    /// The acknowledgement for a server event that asked for one: `43<id>[]`.
    public func encodeAck(id: Int) throws -> String {
        "4" + (try encodeSocketIO(SocketIOPacket(type: .ack, ackID: id, data: Data("[]".utf8))))
    }

    // MARK: Helpers

    static func validateEventName(_ name: String) throws {
        if name.isEmpty {
            throw SocketIOCodecError.invalidEventName("empty")
        }
        if name.utf8.count > maxEventNameBytes {
            throw SocketIOCodecError.invalidEventName("longer than \(maxEventNameBytes) bytes")
        }
        if reservedEventNames.contains(name) {
            throw SocketIOCodecError.invalidEventName("\(name) is reserved by Socket.IO")
        }
    }

    private func checkSize(_ text: String) throws {
        try checkSize(text.utf8.count)
    }

    private func checkSize(_ size: Int) throws {
        if size > maxFrameBytes {
            throw SocketIOCodecError.frameTooLarge(size: size, limit: maxFrameBytes)
        }
    }

    private func jsonObject(_ bytes: ArraySlice<UInt8>) throws -> Any {
        try JSONScanner.checkStructure(bytes, maxDepth: maxNestingDepth)
        do {
            return try JSONSerialization.jsonObject(with: Data(bytes), options: [.fragmentsAllowed])
        } catch {
            throw SocketIOCodecError.malformedJSON("not valid JSON")
        }
    }

    private static func trimmed(_ data: Data) -> Data {
        let bytes = Array(data)
        var start = 0
        var end = bytes.count
        while start < end, JSONScanner.isWhitespace(bytes[start]) { start += 1 }
        while end > start, JSONScanner.isWhitespace(bytes[end - 1]) { end -= 1 }
        return Data(bytes[start..<end])
    }

    private static func digit(_ byte: UInt8) -> Int? {
        guard byte >= UInt8(ascii: "0"), byte <= UInt8(ascii: "9") else { return nil }
        return Int(byte - UInt8(ascii: "0"))
    }

    private static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber else { return nil }
        // Reject booleans, which JSONSerialization also returns as NSNumber.
        if CFGetTypeID(number) == CFBooleanGetTypeID() { return nil }
        let double = number.doubleValue
        // A bound well inside Int's range, so the conversion below can never trap.
        guard double.isFinite, abs(double) < 1e15, double == double.rounded()
        else { return nil }
        return Int(double)
    }

    private static func printable(_ byte: UInt8) -> String {
        if byte >= 0x20, byte < 0x7F {
            return String(UnicodeScalar(byte))
        }
        return String(format: "0x%02X", byte)
    }
}
