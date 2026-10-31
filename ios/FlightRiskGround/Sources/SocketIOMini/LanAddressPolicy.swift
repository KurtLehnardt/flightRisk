import Foundation
#if canImport(Darwin)
import Darwin
#endif

// MARK: - Resolved addresses

/// One resolved IP address as raw bytes (4 for IPv4, 16 for IPv6). The policy
/// decides on these bytes, never on the text the user typed, so numeric
/// spellings such as `0x7f.1`, `2130706433` or `0177.0.0.1` cannot bypass it.
public struct GroundResolvedAddress: Sendable, Equatable, CustomStringConvertible {
    public let bytes: [UInt8]
    /// The IPv6 scope (interface index) for a link-local address, or 0.
    public let scopeID: UInt32

    public init?(bytes: [UInt8], scopeID: UInt32 = 0) {
        guard bytes.count == 4 || bytes.count == 16 else { return nil }
        self.bytes = bytes
        self.scopeID = scopeID
    }

    public var isIPv6: Bool { bytes.count == 16 }

    /// The canonical text of the address (with `%interface` for a scoped IPv6 address).
    public var description: String {
        var buffer = [CChar](repeating: 0, count: Int(INET6_ADDRSTRLEN) + 1)
        let family = isIPv6 ? AF_INET6 : AF_INET
        let text: String = bytes.withUnsafeBytes { raw in
            guard let base = raw.baseAddress, inet_ntop(family, base, &buffer, socklen_t(buffer.count)) != nil else {
                return "?"
            }
            return String(cString: buffer)
        }
        if isIPv6, scopeID != 0 {
            var name = [CChar](repeating: 0, count: Int(IF_NAMESIZE) + 1)
            if if_indextoname(scopeID, &name) != nil {
                return text + "%" + String(cString: name)
            }
            return text + "%\(scopeID)"
        }
        return text
    }
}

/// Resolves a host name to all of its addresses. Tests inject a fake; the app
/// uses `GroundSystemResolver.resolve` (getaddrinfo). It may block, so callers
/// run it off the main thread.
public typealias GroundHostResolver = @Sendable (_ host: String) throws -> [GroundResolvedAddress]

/// The system resolver: `getaddrinfo`, which also reads `.local` (mDNS) names.
public enum GroundSystemResolver {
    public static let resolve: GroundHostResolver = { host in
        var hints = addrinfo()
        hints.ai_family = AF_UNSPEC
        hints.ai_socktype = SOCK_STREAM
        var head: UnsafeMutablePointer<addrinfo>?
        let status = getaddrinfo(host, nil, &hints, &head)
        guard status == 0, let first = head else {
            throw GroundTransportError.failed("the host name did not resolve")
        }
        defer { freeaddrinfo(first) }
        var result: [GroundResolvedAddress] = []
        var cursor: UnsafeMutablePointer<addrinfo>? = first
        while let entry = cursor {
            if let address = entry.pointee.ai_addr {
                if entry.pointee.ai_family == AF_INET {
                    let value = address.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_addr }
                    let bytes = withUnsafeBytes(of: value) { Array($0) }
                    if let resolved = GroundResolvedAddress(bytes: bytes), !result.contains(resolved) {
                        result.append(resolved)
                    }
                } else if entry.pointee.ai_family == AF_INET6 {
                    let socket = address.withMemoryRebound(to: sockaddr_in6.self, capacity: 1) { $0.pointee }
                    let bytes = withUnsafeBytes(of: socket.sin6_addr) { Array($0) }
                    if let resolved = GroundResolvedAddress(bytes: bytes, scopeID: socket.sin6_scope_id), !result.contains(resolved) {
                        result.append(resolved)
                    }
                }
            }
            cursor = entry.pointee.ai_next
        }
        return result
    }
}

// MARK: - Refusal

/// The connection was refused because cleartext (`ws://`, `http://`) would
/// leave the local network. Nothing was sent, and the dashboard API key was
/// never read. `reason` is a sentence for the user and never contains the key.
public struct GroundAddressRefusal: Error, Equatable, Sendable, CustomStringConvertible {
    public let reason: String

    public init(reason: String) {
        self.reason = reason
    }

    /// The sentence shown when the address is not local.
    public static let cleartextMessage = "cleartext is only allowed to local network addresses; use wss:// or https:// for this address"

    /// The refusal for a resolved `address`.
    static func cleartext(address: GroundResolvedAddress) -> GroundAddressRefusal {
        GroundAddressRefusal(reason: cleartextMessage + " (the host resolved to \(address))")
    }

    public var description: String { reason }
}

// MARK: - Policy

/// Cleartext goes only to local network addresses (docs/mobile-contract.md,
/// "Cleartext on the local network"). The Android app enforces the same
/// ranges.
///
/// Allowed: 127/8, 10/8, 172.16/12, 192.168/16, 169.254/16, `::1`, `fc00::/7`,
/// `fe80::/10`, and an IPv4-mapped IPv6 address (`::ffff:a.b.c.d`) whose IPv4
/// part is allowed. Refused: everything else, including 100.64/10 (carrier
/// grade NAT), 0.0.0.0, broadcast, multicast, `::`, and every public address.
///
/// `wss://` and `https://` are encrypted and are not subject to this rule.
/// The app does not pin certificates.
public enum LanAddressPolicy {
    /// True when cleartext may go to `address`.
    public static func isAllowed(_ address: GroundResolvedAddress) -> Bool {
        let b = address.bytes
        if b.count == 4 { return isAllowedIPv4(b) }
        guard b.count == 16 else { return false }
        // IPv4-mapped: ::ffff:a.b.c.d is judged by its IPv4 part.
        if b[0..<10].allSatisfy({ $0 == 0 }), b[10] == 0xff, b[11] == 0xff {
            return isAllowedIPv4(Array(b[12..<16]))
        }
        // ::1
        if b[0..<15].allSatisfy({ $0 == 0 }), b[15] == 1 { return true }
        // fc00::/7 unique local
        if b[0] & 0xfe == 0xfc { return true }
        // fe80::/10 link local
        if b[0] == 0xfe, b[1] & 0xc0 == 0x80 { return true }
        return false
    }

    private static func isAllowedIPv4(_ b: [UInt8]) -> Bool {
        switch (b[0], b[1]) {
        case (127, _), (10, _), (192, 168), (169, 254): return true
        case (172, 16...31): return true
        default: return false
        }
    }

    /// The address a numeric host spells, or nil when `host` is a name. Accepts
    /// the strict forms (`inet_pton`) and the legacy IPv4 spellings that
    /// `getaddrinfo` also accepts (`0x7f.1`, `2130706433`, `0177.0.0.1`).
    public static func numericAddress(_ host: String) -> GroundResolvedAddress? {
        var host = host
        if host.hasPrefix("["), host.hasSuffix("]") { host = String(host.dropFirst().dropLast()) }
        var v6 = in6_addr()
        if host.contains(":") {
            guard inet_pton(AF_INET6, host, &v6) == 1 else { return nil }
            return GroundResolvedAddress(bytes: withUnsafeBytes(of: v6) { Array($0) })
        }
        // inet_aton reads the same legacy spellings as getaddrinfo (a leading 0 is
        // octal, 0x is hex), unlike inet_pton, which reads 010 as decimal 10.
        guard !host.isEmpty, host.allSatisfy({ $0.isHexDigit || $0 == "." || $0 == "x" || $0 == "X" }) else { return nil }
        var v4 = in_addr()
        if inet_aton(host, &v4) == 1 {
            return GroundResolvedAddress(bytes: withUnsafeBytes(of: v4) { Array($0) })
        }
        return nil
    }

    /// Resolves `host` (a numeric literal needs no lookup), refuses cleartext
    /// unless EVERY resolved address is allowed, and returns the address to
    /// connect to: the first one, which is the one that was checked.
    ///
    /// For an encrypted connection (`cleartext == false`) nothing is refused.
    public static func checkedAddress(host: String, cleartext: Bool, resolver: GroundHostResolver) throws -> GroundResolvedAddress {
        let addresses: [GroundResolvedAddress]
        let host = host.hasPrefix("[") && host.hasSuffix("]") ? String(host.dropFirst().dropLast()) : host
        if let literal = numericAddress(host) {
            addresses = [literal]
        } else {
            addresses = try resolver(host)
        }
        guard let first = addresses.first else {
            throw GroundTransportError.failed("the host name did not resolve")
        }
        if cleartext, let refused = addresses.first(where: { !isAllowed($0) }) {
            throw GroundAddressRefusal.cleartext(address: refused)
        }
        return first
    }

    /// The early check for a typed address: a cleartext numeric literal that is
    /// not local is refused at once. Names are checked when connecting.
    public static func earlyRefusal(host: String, secure: Bool) -> GroundAddressRefusal? {
        guard !secure, let literal = numericAddress(host), !isAllowed(literal) else { return nil }
        return GroundAddressRefusal(reason: GroundAddressRefusal.cleartextMessage)
    }
}

// MARK: - Connect plan

/// Where one connection attempt actually goes: the URL rewritten to the
/// checked IP literal, so a second lookup (DNS rebinding) cannot change it.
public struct GroundConnectTarget: Sendable, Equatable {
    /// The URL with the checked IP address as its host.
    public let url: URL
    public let address: GroundResolvedAddress
    /// The name for TLS (SNI and certificate checks) of a `wss://` or `https://` connection to a host name.
    public let tlsServerName: String?
}

public enum GroundConnectPlanner {
    /// Resolves once, applies the policy, and rewrites the URL to the checked address.
    public static func plan(url: URL, resolver: GroundHostResolver) throws -> GroundConnectTarget {
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              let host = components.host, !host.isEmpty
        else {
            throw GroundConfigurationError.invalidURL(url.absoluteString)
        }
        let encrypted = scheme == "wss" || scheme == "https"
        let address = try LanAddressPolicy.checkedAddress(host: host, cleartext: !encrypted, resolver: resolver)
        let isLiteral = LanAddressPolicy.numericAddress(host) != nil
        let text = address.description
        if address.isIPv6, address.scopeID != 0 {
            components.percentEncodedHost = "[" + text.replacingOccurrences(of: "%", with: "%25") + "]"
        } else {
            components.host = address.isIPv6 ? "[" + text + "]" : text
        }
        guard let rewritten = components.url else {
            throw GroundConfigurationError.invalidURL(url.absoluteString)
        }
        return GroundConnectTarget(url: rewritten, address: address, tlsServerName: encrypted && !isLiteral ? host : nil)
    }

    /// Like `plan(url:resolver:)`, with the lookup on a background queue.
    public static func planOffMainThread(url: URL, resolver: @escaping GroundHostResolver) async throws -> GroundConnectTarget {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                continuation.resume(with: Result { try plan(url: url, resolver: resolver) })
            }
        }
    }
}
