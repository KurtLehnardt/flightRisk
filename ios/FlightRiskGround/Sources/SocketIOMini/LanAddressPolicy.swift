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
/// For a cleartext host NAME the rule is: connect only to the allowed answers,
/// and refuse only when none of the answers is allowed. A dual-stack `.local`
/// or router name often publishes a global IPv6 address next to its private
/// ones; that public answer is simply never connected to, because every socket
/// goes to an address that was checked (never to the name). The Android policy
/// is stricter and refuses a name when ANY answer is public.
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

    /// The address a numeric host spells, or nil when `host` is a name or is a
    /// spelling this code does not read itself. IPv4 is read only as four plain
    /// decimal numbers (no leading zeros, no hex, octal or short forms) and IPv6
    /// with `inet_pton`. Any other spelling returns nil, so it goes to the system
    /// resolver and is judged by the bytes the resolver returns. (On macOS,
    /// `inet_pton`, `inet_aton` and `getaddrinfo` disagree about leading zeros,
    /// which is why this parser does not accept them.)
    public static func numericAddress(_ host: String) -> GroundResolvedAddress? {
        var host = host
        if host.hasPrefix("["), host.hasSuffix("]") { host = String(host.dropFirst().dropLast()) }
        if host.contains(":") {
            var v6 = in6_addr()
            guard inet_pton(AF_INET6, host, &v6) == 1 else { return nil }
            return GroundResolvedAddress(bytes: withUnsafeBytes(of: v6) { Array($0) })
        }
        guard let bytes = plainDottedQuad(host) else { return nil }
        return GroundResolvedAddress(bytes: bytes)
    }

    /// Four decimal numbers 0 to 255 separated by dots, without leading zeros.
    static func plainDottedQuad(_ text: String) -> [UInt8]? {
        let parts = text.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return nil }
        var bytes: [UInt8] = []
        for part in parts {
            guard !part.isEmpty, part.count <= 3, part.allSatisfy({ $0 >= "0" && $0 <= "9" }),
                  part == "0" || part.first != "0", let value = UInt8(part)
            else { return nil }
            bytes.append(value)
        }
        return bytes
    }

    /// True for a spelling that the system would read as an IPv4 address but that
    /// is not four plain decimal numbers (`0x7f.1`, `2130706433`, `192.168.001.010`).
    public static func isLegacyIPv4Spelling(_ host: String) -> Bool {
        guard plainDottedQuad(host) == nil, !host.isEmpty,
              host.allSatisfy({ $0.isHexDigit || $0 == "." || $0 == "x" || $0 == "X" })
        else { return false }
        var v4 = in_addr()
        return inet_aton(host, &v4) == 1
    }

    /// The addresses a connection may be made to, in the order to try them: IPv4
    /// first, then IPv6, each group in the resolver's order.
    ///
    /// - A numeric literal needs no lookup. For cleartext it must be allowed.
    /// - A name is resolved once. For cleartext only the allowed answers are kept
    ///   and the name is refused when none is allowed.
    /// - For an encrypted connection (`cleartext == false`) nothing is refused.
    public static func checkedAddresses(host: String, cleartext: Bool, resolver: GroundHostResolver) throws -> [GroundResolvedAddress] {
        let host = host.hasPrefix("[") && host.hasSuffix("]") ? String(host.dropFirst().dropLast()) : host
        let answers: [GroundResolvedAddress]
        if let literal = numericAddress(host) {
            answers = [literal]
        } else {
            do {
                answers = try resolver(host)
            } catch let error as GroundAddressRefusal {
                throw error
            } catch {
                throw GroundLookupError.notFound(host: host)
            }
        }
        guard !answers.isEmpty else { throw GroundLookupError.notFound(host: host) }
        let usable = cleartext ? answers.filter(isAllowed) : answers
        guard !usable.isEmpty else { throw GroundAddressRefusal.cleartext(address: answers[0]) }
        return usable.filter { !$0.isIPv6 } + usable.filter(\.isIPv6)
    }

    /// The early check for a typed address: a cleartext numeric literal that is
    /// not local is refused at once. Names are checked when connecting.
    public static func earlyRefusal(host: String, secure: Bool) -> GroundAddressRefusal? {
        guard !secure, let literal = numericAddress(host), !isAllowed(literal) else { return nil }
        return GroundAddressRefusal(reason: GroundAddressRefusal.cleartextMessage)
    }
}

// MARK: - Lookup errors

/// The host name could not be looked up. `description` is a sentence for the user.
public enum GroundLookupError: Error, Equatable, Sendable, CustomStringConvertible {
    case notFound(host: String)
    case timedOut(host: String)

    public var description: String {
        switch self {
        case let .notFound(host): return "Could not find \(host) on the network"
        case let .timedOut(host): return "Looking up \(host) timed out"
        }
    }
}

// MARK: - Connect plan

/// Where a connection attempt goes.
///
/// Cleartext (`ws://`, `http://`): `attempts` holds one URL per checked address,
/// each with the IP literal as its host, in the order to try. The name is never
/// looked up again, so DNS rebinding cannot change the destination.
/// `hostHeader` is the original name (IPv6 in brackets) with the port, for the
/// HTTP `Host` header.
///
/// Encrypted (`wss://`, `https://`): `attempts` is the original URL. The
/// connection is made by name, so TLS authenticates the name, the `Host`
/// header is unchanged and the system can try several addresses.
public struct GroundConnectTarget: Sendable, Equatable {
    public let attempts: [URL]
    public let addresses: [GroundResolvedAddress]
    public let hostHeader: String?
    public var isCleartext: Bool { hostHeader != nil }
}

public enum GroundConnectPlanner {
    /// Resolves once and applies the policy.
    public static func plan(url: URL, resolver: GroundHostResolver) throws -> GroundConnectTarget {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              let rawHost = components.host, !rawHost.isEmpty
        else {
            throw GroundConfigurationError.invalidURL(url.absoluteString)
        }
        if scheme == "wss" || scheme == "https" {
            return GroundConnectTarget(attempts: [url], addresses: [], hostHeader: nil)
        }
        let host = rawHost.hasPrefix("[") && rawHost.hasSuffix("]") ? String(rawHost.dropFirst().dropLast()) : rawHost
        let addresses = try LanAddressPolicy.checkedAddresses(host: host, cleartext: true, resolver: resolver)
        var attempts: [URL] = []
        for address in addresses {
            var copy = components
            let text = address.description
            if address.isIPv6, address.scopeID != 0 {
                copy.percentEncodedHost = "[" + text.replacingOccurrences(of: "%", with: "%25") + "]"
            } else {
                copy.host = address.isIPv6 ? "[" + text + "]" : text
            }
            guard let rewritten = copy.url else { throw GroundConfigurationError.invalidURL(url.absoluteString) }
            attempts.append(rewritten)
        }
        let shownHost = host.contains(":") ? "[\(host)]" : host
        let header = components.port.map { "\(shownHost):\($0)" } ?? shownHost
        return GroundConnectTarget(attempts: attempts, addresses: addresses, hostHeader: header)
    }

    /// Like `plan(url:resolver:)`, with the lookup on its own background queue, so the
    /// caller never waits on `getaddrinfo`: after `timeout` seconds, or when the task is
    /// cancelled or `lookup.cancel()` is called, it returns at once and the lookup is
    /// left to finish unobserved.
    public static func planOffMainThread(
        url: URL,
        resolver: @escaping GroundHostResolver,
        timeout: TimeInterval = 10,
        lookup: GroundLookup = GroundLookup()
    ) async throws -> GroundConnectTarget {
        try await lookup.run(url: url, resolver: resolver, timeout: timeout)
    }
}

/// One name lookup that the caller can stop waiting for.
public final class GroundLookup: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<GroundConnectTarget, Error>?
    private var cancelled = false

    public init() {}

    /// Makes a pending (or the next) `run` throw `GroundTransportError.closed`.
    public func cancel() {
        let pending: CheckedContinuation<GroundConnectTarget, Error>? = lock.withLock {
            cancelled = true
            defer { continuation = nil }
            return continuation
        }
        pending?.resume(throwing: GroundTransportError.closed(code: nil))
    }

    private func finish(_ result: Result<GroundConnectTarget, Error>) {
        let pending: CheckedContinuation<GroundConnectTarget, Error>? = lock.withLock {
            defer { continuation = nil }
            return continuation
        }
        pending?.resume(with: result)
    }

    func run(url: URL, resolver: @escaping GroundHostResolver, timeout: TimeInterval) async throws -> GroundConnectTarget {
        let host = URLComponents(url: url, resolvingAgainstBaseURL: false)?.host ?? ""
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<GroundConnectTarget, Error>) in
                let startNow = lock.withLock { () -> Bool in
                    if cancelled { return false }
                    self.continuation = continuation
                    return true
                }
                guard startNow else {
                    continuation.resume(throwing: GroundTransportError.closed(code: nil))
                    return
                }
                DispatchQueue.global(qos: .userInitiated).async { [self] in
                    finish(Result { try GroundConnectPlanner.plan(url: url, resolver: resolver) })
                }
                DispatchQueue.global().asyncAfter(deadline: .now() + max(0.05, timeout)) { [self] in
                    finish(.failure(GroundLookupError.timedOut(host: host)))
                }
            }
        } onCancel: {
            cancel()
        }
    }
}
