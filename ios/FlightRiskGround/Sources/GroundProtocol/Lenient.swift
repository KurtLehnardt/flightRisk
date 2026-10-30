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

/// A collection that decodes element by element, skipping elements that do not decode, so one
/// bad entry never drops the whole collection.
protocol LenientCollection {
    static func decodeLenient(from decoder: Decoder) -> Any?
}

/// Accepts any JSON value without failing; used to step past an element that did not decode.
private struct SkipValue: Decodable {
    init(from decoder: Decoder) throws {}
}

private struct DynamicKey: CodingKey {
    var stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

extension Array: LenientCollection where Element: Decodable {
    static func decodeLenient(from decoder: Decoder) -> Any? {
        guard var c = try? decoder.unkeyedContainer() else { return nil }
        var out: [Element] = []
        while !c.isAtEnd {
            if Element.self == Int.self {
                // Same 32-bit whole-number rule as an Int field.
                // Lenient never throws, so the element is consumed either way.
                if let l = try? c.decode(Lenient<Int>.self) {
                    if let v = l.wrappedValue as? Element { out.append(v) }
                } else { break }
            } else if let e = try? c.decode(Element.self) { out.append(e) } else { _ = try? c.decode(SkipValue.self) }
        }
        return out
    }
}

extension Dictionary: LenientCollection where Key == String, Value: Decodable {
    static func decodeLenient(from decoder: Decoder) -> Any? {
        guard let c = try? decoder.container(keyedBy: DynamicKey.self) else { return nil }
        var out: [String: Value] = [:]
        for key in c.allKeys {
            if Value.self == Int.self {
                if let v = (try? c.decode(Lenient<Int>.self, forKey: key))?.wrappedValue as? Value { out[key.stringValue] = v }
            } else if let v = try? c.decode(Value.self, forKey: key) { out[key.stringValue] = v }
        }
        return out
    }
}

/// Property wrapper for a wire field the server may omit, send as null, or send with an
/// unexpected type. Every such case decodes to nil and never throws, so one odd field can never
/// make a whole payload undecodable. Types are strict, as in the dashboard and the Android app:
/// a number is never read from a string, a string never from a number, a boolean never from 0
/// or 1. An Int field accepts a whole number that fits in 32 bits and reads anything else
/// (fractional, huge, non-numeric) as nil, never trapping. Arrays and string-keyed maps decode
/// element by element and skip the elements that do not decode.
@propertyWrapper
public struct Lenient<Value: Codable & Equatable & Sendable>: Codable, Equatable, Sendable {
    public var wrappedValue: Value?

    public init(wrappedValue: Value?) { self.wrappedValue = wrappedValue }
    public init() { self.wrappedValue = nil }

    public init(from decoder: Decoder) throws {
        wrappedValue = nil
        guard let c = try? decoder.singleValueContainer(), !c.decodeNil() else { return }
        if Value.self == Int.self {
            if let d = try? c.decode(Double.self), d.isFinite, d == d.rounded(.down),
               d >= Double(Int32.min), d <= Double(Int32.max) {
                wrappedValue = Int(d) as? Value
            }
        } else if let collection = Value.self as? any LenientCollection.Type {
            wrappedValue = collection.decodeLenient(from: decoder) as? Value
        } else {
            wrappedValue = try? c.decode(Value.self)
        }
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
        // A value that is not a string is not a vocabulary value: throwing here makes a Lenient
        // field read as nil, so a wrong-typed `event` or `reason` never hides a valid fallback.
        let c = try decoder.singleValueContainer()
        self.init(wire: try c.decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(wire)
    }

    /// True when this build has a named case for the value.
    public var isKnown: Bool { Self.knownWireValues.contains(wire) }
}
