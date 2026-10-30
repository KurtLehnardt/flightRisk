import Foundation

/// A small, iterative scanner for the structure of JSON text.
///
/// The scanner never recurses, so a hostile frame with millions of nested
/// brackets cannot overflow the stack. It checks only the bracket structure,
/// string boundaries and nesting depth; full validation of each value is left
/// to `JSONSerialization`, which is only ever called after this scanner has
/// proved that the nesting depth is within the limit.
enum JSONScanner {
    private static let quote = UInt8(ascii: "\"")
    private static let backslash = UInt8(ascii: "\\")
    private static let openBracket = UInt8(ascii: "[")
    private static let closeBracket = UInt8(ascii: "]")
    private static let openBrace = UInt8(ascii: "{")
    private static let closeBrace = UInt8(ascii: "}")
    private static let comma = UInt8(ascii: ",")

    static func isWhitespace(_ byte: UInt8) -> Bool {
        byte == 0x20 || byte == 0x0A || byte == 0x0D || byte == 0x09
    }

    /// Throws when the nesting depth of `bytes` exceeds `maxDepth` or when the
    /// brackets outside strings do not balance.
    static func checkStructure(_ bytes: ArraySlice<UInt8>, maxDepth: Int) throws {
        var stack: [UInt8] = []
        var inString = false
        var escaped = false
        for byte in bytes {
            if inString {
                if escaped {
                    escaped = false
                } else if byte == backslash {
                    escaped = true
                } else if byte == quote {
                    inString = false
                }
                continue
            }
            switch byte {
            case quote:
                inString = true
            case openBracket, openBrace:
                stack.append(byte)
                if stack.count > maxDepth {
                    throw SocketIOCodecError.nestingTooDeep(limit: maxDepth)
                }
            case closeBracket, closeBrace:
                guard let open = stack.popLast(), matches(open: open, close: byte) else {
                    throw SocketIOCodecError.malformedJSON("unbalanced brackets")
                }
            default:
                break
            }
        }
        if inString {
            throw SocketIOCodecError.malformedJSON("unterminated string")
        }
        if !stack.isEmpty {
            throw SocketIOCodecError.malformedJSON("unclosed bracket")
        }
    }

    /// Splits a top-level JSON array into the byte ranges of its elements,
    /// with surrounding whitespace trimmed. The ranges index into `bytes`.
    static func splitTopLevelArray(
        _ bytes: ArraySlice<UInt8>,
        maxDepth: Int,
        maxElements: Int
    ) throws -> [Range<Int>] {
        var index = bytes.startIndex
        while index < bytes.endIndex, isWhitespace(bytes[index]) {
            index += 1
        }
        guard index < bytes.endIndex, bytes[index] == openBracket else {
            throw SocketIOCodecError.malformedJSON("expected a JSON array")
        }
        index += 1

        var stack: [UInt8] = [openBracket]
        var inString = false
        var escaped = false
        var elementStart: Int?
        var lastSignificant = index
        var sawComma = false
        var elements: [Range<Int>] = []

        func finishElement(at end: Int) throws {
            guard let start = elementStart else {
                throw SocketIOCodecError.malformedJSON("empty array element")
            }
            elements.append(start..<end)
            if elements.count > maxElements {
                throw SocketIOCodecError.tooManyArguments(limit: maxElements)
            }
            elementStart = nil
        }

        while index < bytes.endIndex {
            let byte = bytes[index]
            if inString {
                if escaped {
                    escaped = false
                } else if byte == backslash {
                    escaped = true
                } else if byte == quote {
                    inString = false
                    lastSignificant = index + 1
                }
                index += 1
                continue
            }
            switch byte {
            case quote:
                if stack.count == 1, elementStart == nil { elementStart = index }
                inString = true
            case openBracket, openBrace:
                if stack.count == 1, elementStart == nil { elementStart = index }
                stack.append(byte)
                if stack.count > maxDepth {
                    throw SocketIOCodecError.nestingTooDeep(limit: maxDepth)
                }
            case closeBracket, closeBrace:
                guard let open = stack.popLast(), matches(open: open, close: byte) else {
                    throw SocketIOCodecError.malformedJSON("unbalanced brackets")
                }
                if stack.isEmpty {
                    // The top-level array closed.
                    if elementStart != nil {
                        try finishElement(at: lastSignificant)
                    } else if sawComma {
                        throw SocketIOCodecError.malformedJSON("trailing comma")
                    }
                    var rest = index + 1
                    while rest < bytes.endIndex {
                        guard isWhitespace(bytes[rest]) else {
                            throw SocketIOCodecError.malformedJSON("trailing data after the array")
                        }
                        rest += 1
                    }
                    return elements
                }
                lastSignificant = index + 1
                index += 1
                continue
            case comma where stack.count == 1:
                try finishElement(at: lastSignificant)
                sawComma = true
                index += 1
                continue
            default:
                if isWhitespace(byte) {
                    index += 1
                    continue
                }
                if stack.count == 1, elementStart == nil { elementStart = index }
            }
            if !inString {
                lastSignificant = index + 1
            }
            index += 1
        }
        throw SocketIOCodecError.malformedJSON("truncated array")
    }

    private static func matches(open: UInt8, close: UInt8) -> Bool {
        (open == openBracket && close == closeBracket) || (open == openBrace && close == closeBrace)
    }
}
