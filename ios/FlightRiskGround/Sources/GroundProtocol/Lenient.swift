import Foundation

/// A JSON value of any shape. Used for fields whose content the phone only passes along.
public enum JSONValue: Codable, Equatable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .null }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }
}

/// Property wrapper for a wire field the server may omit, send as null, or send with an
/// unexpected type. Every such case decodes to nil and never throws, so one odd field can never
/// make a whole payload undecodable. Numbers are accepted for String fields and numeric strings
/// for Double and Int fields, as the dashboard's JavaScript does.
@propertyWrapper
public struct Lenient<Value: Codable & Equatable & Sendable>: Codable, Equatable, Sendable {
    public var wrappedValue: Value?

    public init(wrappedValue: Value?) { self.wrappedValue = wrappedValue }
    public init() { self.wrappedValue = nil }

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { wrappedValue = nil; return }
        if let v = try? c.decode(Value.self) { wrappedValue = v; return }
        if Value.self == String.self {
            if let i = try? c.decode(Int.self) { wrappedValue = String(i) as? Value; return }
            if let d = try? c.decode(Double.self) { wrappedValue = String(d) as? Value; return }
        } else if Value.self == Double.self {
            if let s = try? c.decode(String.self), let d = Double(s) { wrappedValue = d as? Value; return }
        } else if Value.self == Int.self {
            if let s = try? c.decode(String.self), let i = Int(s) { wrappedValue = i as? Value; return }
            if let d = try? c.decode(Double.self), d.rounded() == d { wrappedValue = Int(d) as? Value; return }
        }
        wrappedValue = nil
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        if let v = wrappedValue { try c.encode(v) } else { try c.encodeNil() }
    }
}

extension KeyedDecodingContainer {
    /// A missing key decodes to nil instead of throwing keyNotFound.
    public func decode<Value>(_ type: Lenient<Value>.Type, forKey key: Key) throws -> Lenient<Value> {
        (try? decodeIfPresent(Lenient<Value>.self, forKey: key)) ?? Lenient<Value>(wrappedValue: nil)
    }
}

/// A string-backed enumeration with an `.unknown(String)` case. Unknown wire values are kept
/// verbatim, so they can be shown by name and are never a decoding failure.
public protocol WireEnum: Codable, Equatable, Hashable, Sendable {
    init(wire: String)
    var wire: String { get }
    /// Every wire value this build knows about, in no particular order.
    static var knownWireValues: [String] { get }
}

extension WireEnum {
    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        self.init(wire: (try? c.decode(String.self)) ?? "")
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(wire)
    }

    /// True when this build has a named case for the value.
    public var isKnown: Bool { Self.knownWireValues.contains(wire) }
}
