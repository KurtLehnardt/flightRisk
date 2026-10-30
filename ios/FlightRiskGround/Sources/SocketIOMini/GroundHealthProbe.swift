import Foundation

/// The outcome of a health probe ("Test connection" in Settings).
public enum GroundHealthResult: Sendable, Equatable, CustomStringConvertible {
    /// The ground answered and accepted the key. `version` comes from `/api/health`.
    case ok(version: String?)
    /// The ground answered but `/api/status` returned 401: a wrong or missing key.
    case wrongKey(version: String?)
    /// The ground could not be reached.
    case unreachable(String)
    /// The ground answered with an unexpected HTTP status (a redirect included:
    /// redirects are never followed).
    case unexpectedStatus(Int)
    /// The ground answered 200 but not with the JSON a FlightRisk dashboard sends.
    case invalidResponse(String)

    public var description: String {
        switch self {
        case let .ok(version): return "reachable, key accepted" + (version.map { ", version \($0)" } ?? "")
        case let .wrongKey(version): return "reachable, wrong key" + (version.map { ", version \($0)" } ?? "")
        case let .unreachable(detail): return "unreachable: \(detail)"
        case let .unexpectedStatus(status): return "unexpected HTTP status \(status)"
        case let .invalidResponse(detail): return "not a FlightRisk dashboard response: \(detail)"
        }
    }
}

/// Checks that a ground answers and accepts the key, without opening a socket:
/// `GET /api/health` (public; returns `version`) and `GET /api/status` (needs
/// the key; 401 means a wrong key). The key goes only in the
/// `Authorization: Bearer` header, never in the URL. Redirects are not
/// followed, and "key accepted" needs `/api/status` to answer 200 with the
/// dashboard's own JSON object (`running` and `source`).
public struct GroundHealthProbe: Sendable {
    public let endpoint: GroundEndpoint
    private let apiKey: GroundAPIKey
    private let http: any GroundHTTPClient
    private let timeout: TimeInterval

    public init(endpoint: GroundEndpoint, apiKey: GroundAPIKey, http: any GroundHTTPClient = URLSessionGroundHTTPClient(), timeout: TimeInterval = 5) {
        self.endpoint = endpoint
        self.apiKey = apiKey
        self.http = http
        self.timeout = timeout
    }

    public func run() async -> GroundHealthResult {
        let redact = GroundRedactor(key: apiKey)
        var headers: [String: String] = ["Accept": "application/json"]
        if !apiKey.isEmpty {
            headers["Authorization"] = "Bearer \(apiKey.secret)"
        }
        let base = endpoint.httpBaseURL
        var version: String?
        do {
            let health = try await http.get(base.appendingPathComponent("api/health"), headers: headers, timeout: timeout)
            guard health.status == 200 else {
                return .unexpectedStatus(health.status)
            }
            guard let object = try? JSONSerialization.jsonObject(with: health.body) as? [String: Any],
                  object["status"] is String
            else {
                return .invalidResponse("/api/health did not return a status object")
            }
            version = object["version"] as? String
            let status = try await http.get(base.appendingPathComponent("api/status"), headers: headers, timeout: timeout)
            switch status.status {
            case 200:
                guard let object = try? JSONSerialization.jsonObject(with: status.body) as? [String: Any],
                      object["running"] is Bool, object["source"] != nil
                else {
                    return .invalidResponse("/api/status did not return the dashboard status object")
                }
                return .ok(version: version)
            case 401, 403: return .wrongKey(version: version)
            default: return .unexpectedStatus(status.status)
            }
        } catch {
            return .unreachable(redact(Self.describe(error)))
        }
    }

    static func describe(_ error: any Error) -> String {
        if let urlError = error as? URLError {
            // The code alone: URLError's userInfo carries the failing URL, which is
            // harmless here but kept out of the text on principle.
            return "URLError \(urlError.code.rawValue)"
        }
        if case let .bodyTooLarge(limit) = error as? GroundHTTPError {
            return "response body larger than \(limit) bytes"
        }
        return String(describing: type(of: error))
    }
}
