import Foundation
import GroundProtocol
import os
import Security
import SocketIOMini
import UIKit

// The Ground tab's state: one connection to a FlightRisk ground station (the dashboard's
// Socket.IO contract, docs/mobile-contract.md), the drones as the ground reports them, and the
// three commands the phone may send that move a drone (LAND ALL, land one drone, resume a held
// drone's search).
//
// Safety rules this file keeps (docs/mobile-contract.md, "Safety display rules"):
// - LAND ALL and per-drone Land are never disabled by any drone, fleet or connection state.
//   When the link is down a tap says plainly that nothing was sent; nothing is ever queued.
// - Every result is shown. "Sent" only means "handed to the connection" until the ground's
//   answer arrives; a missing answer turns into a visible "no reply" or "no final answer"
//   state, and a link lost while a command waits turns into a visible failure.
// - Any refused or failed manual reply, whatever its action (an orphan auto-land included), is
//   shown on the drone's card until dismissed.
// - No command is retried automatically.
// - Drone state comes only from `DroneStateReducer`, fed with every server event in arrival
//   order. State from before a disconnect is kept but marked stale ("LAST: ...", "last value")
//   until a fresh `fleet_status` arrives.
// - The API key goes from the Keychain straight into `GroundAPIKey` and from there only into
//   the client. It is never stored in this object, shown, or logged.

// MARK: - Settings storage

/// The result of reading the dashboard API key.
enum GroundKeyLookup: Equatable {
    case found(GroundAPIKey)
    /// No key is saved.
    case notFound
    /// A key may be saved but cannot be read now (for example the phone is locked).
    case unavailable
}

/// Whether a key is saved, as the views show it. Never the key itself.
enum GroundKeyState: Equatable {
    case saved
    case none
    case unavailable
}

/// Where the dashboard API key is kept. Only the Keychain implementation is used in the app.
protocol GroundKeyStore: Sendable {
    func lookupKey() -> GroundKeyLookup
    func saveKey(_ key: String) throws
    func deleteKey()
}

extension GroundKeyStore {
    var keyState: GroundKeyState {
        switch lookupKey() {
        case .found: return .saved
        case .notFound: return .none
        case .unavailable: return .unavailable
        }
    }

    /// The saved key wrapped for the client, or nil when none can be read.
    func loadKey() -> GroundAPIKey? {
        if case let .found(key) = lookupKey() { return key }
        return nil
    }
}

/// The dashboard API key in the Keychain, saved and deleted through `KeychainHelper` with
/// provider `ground`.
///
/// The contract names this secret `FLIGHTRISK_DASHBOARD_API_KEY` (the ground's
/// `FLIGHTRISK_API_KEY`). `KeychainHelper` names its items `flightrisk_api_key_<provider>`,
/// so the item's account is `flightrisk_api_key_ground` in the service `com.flightrisk.app`,
/// stored with `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`. Reading uses its own query on
/// the same item, because `KeychainHelper.load` cannot tell "no key" from "cannot read now".
struct KeychainGroundKeyStore: GroundKeyStore {
    static let provider = "ground"
    static let contractSecretName = "FLIGHTRISK_DASHBOARD_API_KEY"
    /// Must match `KeychainHelper`'s service and account naming.
    static let service = "com.flightrisk.app"
    static let account = "flightrisk_api_key_\(provider)"

    func lookupKey() -> GroundKeyLookup {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: Self.account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        return Self.lookup(status: status, data: result as? Data)
    }

    /// Maps a Keychain read to a lookup. Only "item not found" means no key; every other
    /// failure (errSecInteractionNotAllowed while the phone is locked, among others) means the
    /// key cannot be read now.
    static func lookup(status: OSStatus, data: Data?) -> GroundKeyLookup {
        switch status {
        case errSecSuccess:
            guard let data, let text = String(data: data, encoding: .utf8), !text.isEmpty else { return .notFound }
            return .found(GroundAPIKey(text))
        case errSecItemNotFound:
            return .notFound
        default:
            return .unavailable
        }
    }

    func saveKey(_ key: String) throws {
        try KeychainHelper.saveApiKey(key, provider: Self.provider)
    }

    func deleteKey() {
        KeychainHelper.deleteApiKey(provider: Self.provider)
    }
}

/// The non-secret connection settings: the ground address and whether the link is switched on.
struct GroundConnectionSettings {
    static let addressKey = "flightrisk_ground_address"
    static let enabledKey = "flightrisk_ground_enabled"

    let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    /// The normalised address (`ws://HOST:PORT`), or nil when none is saved.
    var address: String? {
        get { defaults.string(forKey: Self.addressKey).flatMap { $0.isEmpty ? nil : $0 } }
        nonmutating set { defaults.set(newValue, forKey: Self.addressKey) }
    }

    var enabled: Bool {
        get { defaults.bool(forKey: Self.enabledKey) }
        nonmutating set { defaults.set(newValue, forKey: Self.enabledKey) }
    }
}

/// Validation of the ground station address typed in Settings.
enum GroundAddress {
    /// Accepts `ws://`, `wss://`, `http://`, `https://` or a bare `HOST[:PORT]`; the port
    /// defaults to 5555. Refuses an empty text, a path, a query, user info, a bad host or a
    /// port outside 1 to 65535. The result's `description` is the normalised `ws://HOST:PORT`.
    static func validate(_ text: String) -> Result<GroundEndpoint, GroundAddressError> {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return .failure(.empty) }
        do {
            return .success(try GroundEndpoint.parse(trimmed))
        } catch let error as GroundConfigurationError {
            if case let .cleartextNotAllowed(reason) = error { return .failure(.refused(reason: reason, message: error.description)) }
            return .failure(.invalid(error.description))
        } catch {
            return .failure(.invalid("\"\(trimmed)\" is not a ground station address of the form ws://HOST:PORT."))
        }
    }
}

enum GroundAddressError: Error, Equatable {
    case empty
    case invalid(String)
    /// A cleartext address that is not on the local network. `reason` is the sentence;
    /// `message` is what Settings shows.
    case refused(reason: String, message: String)

    var message: String {
        switch self {
        case .empty: return "Enter the ground station address, for example ws://192.168.1.20:5555."
        case let .invalid(text): return text
        case let .refused(_, message): return message
        }
    }
}

// MARK: - View state

/// The connection as the Ground tab shows it.
enum GroundLinkStatus: Equatable {
    /// No address or no key saved yet.
    case notConfigured
    /// A key may be saved, but the Keychain cannot be read now (the phone is locked).
    case keychainLocked
    /// Configured, but the user switched the link off.
    case off
    /// The app is in the background; the link is closed until it returns.
    case paused
    case connecting(attempt: Int)
    case connected
    case reconnecting(attempt: Int, retryInSeconds: Double, reason: String)
    /// The saved address is a cleartext address that is not on the local network; nothing is sent to it.
    case addressRefused(String)
    /// The ground refused the key. Distinct from network loss; the client waits for the user.
    case wrongKey
    case stopped

    var isConnected: Bool { self == .connected }

    var text: String {
        switch self {
        case .notConfigured: return "Not configured: add the ground station address and key in Settings."
        case .keychainLocked: return "Keychain locked: unlock the phone to connect to the ground station."
        case .off: return "Ground link off. Switch it on in Settings."
        case let .addressRefused(reason): return "Address refused, nothing is sent: \(GroundText.shorten(reason)). Change the address in Settings."
        case .paused: return "Not connected: the app was in the background."
        case let .connecting(attempt): return attempt > 1 ? "Connecting (attempt \(attempt))\u{2026}" : "Connecting\u{2026}"
        case .connected: return "Connected"
        case let .reconnecting(attempt, retryIn, reason):
            return "Reconnecting (attempt \(attempt), next try in \(GroundText.number(retryIn)) s): \(reason)"
        case .wrongKey: return "Wrong key: the ground station refused the API key. Check the key in Settings."
        case .stopped: return "Not connected."
        }
    }

    /// The short label of the live pill.
    var pillText: String {
        switch self {
        case .connected: return "LIVE"
        case .connecting: return "CONNECTING"
        case .reconnecting: return "RECONNECTING"
        case .wrongKey: return "WRONG KEY"
        case .keychainLocked: return "LOCKED"
        case .notConfigured: return "NOT SET UP"
        case .addressRefused: return "REFUSED"
        case .off: return "OFF"
        case .paused, .stopped: return "OFFLINE"
        }
    }
}

/// The visible result of one command, or of a failure the ground reported for a drone.
struct GroundCommandFeedback: Equatable {
    enum Phase: Equatable {
        /// The tap was taken; the command is on its way to the connection.
        case sending
        /// Handed to the connection. Not proof of delivery: the ground's answer is.
        case handedOver
        /// The ground accepted it. When `awaitsFinalAnswer` is true its final answer
        /// (`manual_result`) is still due and times out into a failure.
        case accepted
        /// No answer within the reply timeout. The operator may tap again.
        case noReplyYet
        case succeeded
        /// Reached only some drones: a warning that never fades.
        case partial
        case failed
    }

    /// Distinguishes taps, including two taps within the same instant.
    let attempt: Int
    var phase: Phase
    var text: String
    var detail: String?
    let startedAt: Date
    var updatedAt: Date
    var awaitsFinalAnswer = true

    var isFailure: Bool { phase == .failed }
    var isWarning: Bool { phase == .failed || phase == .partial || phase == .noReplyYet }
    /// Still waiting for the ground (or the edge) to answer.
    var isWaiting: Bool {
        switch phase {
        case .sending, .handedOver, .noReplyYet: return true
        case .accepted: return awaitsFinalAnswer
        case .succeeded, .partial, .failed: return false
        }
    }
    /// Whether to offer a "send again" button next to the text. A second tap is simply
    /// another command; nothing is retried automatically.
    var offersRetry: Bool { phase == .noReplyYet || phase == .failed || phase == .partial }
}

/// A `fleet_event` banner or a refused plan or start.
struct GroundBanner: Identifiable, Equatable {
    let id: UUID
    let text: String
    let isFailure: Bool
    let receivedAt: Date
}

/// An `error` or `warning` from the ground. Errors stay until dismissed; warnings fade.
struct GroundToast: Identifiable, Equatable {
    let id: UUID
    let text: String
    let isError: Bool
    let receivedAt: Date
}

/// Everything one drone status card shows. Built from `DroneStateReducer.cards`.
struct GroundDroneCardModel: Identifiable, Equatable {
    let card: DroneCardState
    /// "CONNECTED", or "LAST: CONNECTED" while the whole state is stale.
    let linkChipText: String
    /// True when the link chip must not use its live colour (the state is stale).
    let linkChipGreyed: Bool
    /// "MANUAL", "HELD" or nil.
    let controlChip: String?
    /// The control text with the HELD countdown counted down locally; empty for none. Ends in
    /// "(last value)" while the state is stale.
    let controlText: String
    /// "87%", "87% (last value)" or "--".
    let batteryText: String
    /// "100 cm", "100 cm (last value)" or "--".
    let altitudeText: String
    let isPartial: Bool
    /// True when the whole state is stale (not connected, or waiting for a fresh fleet status).
    let isStateStale: Bool
    /// True when the values are not live: the whole state is stale or the drone is DOWN or LOST.
    let isStale: Bool
    /// Why the values are not live, or nil.
    let staleNote: String?
    let canResume: Bool
    let landFeedback: GroundCommandFeedback?
    let resumeFeedback: GroundCommandFeedback?
    /// A failure the ground reported for this drone (for example a failed orphan auto-land).
    let alert: GroundCommandFeedback?
    /// The feedback texts with their age, as the card shows them.
    let landFeedbackText: String?
    let resumeFeedbackText: String?
    let alertText: String?

    var id: String { card.id }
    /// The id as shown: cut to 40 characters. `id` stays the real id that commands carry.
    var displayId: String { GroundText.shortId(card.id) }
}

/// Text the Ground tab adds to `GroundText`.
enum GroundUIText {
    private static let landAllFallback = "Land the drones now with their own controls, or press LAND ALL on the ground laptop."
    static let landAllHandedOver = GroundText.landAllSending + " Handed to the connection; waiting for the ground's reply."
    static let landAllNotSentWrongKey = "LAND ALL was NOT sent: the ground refused this phone's key. " + landAllFallback
    static let landAllNotSentLinkOff = "LAND ALL was NOT sent: this phone's ground link is off or not set up. " + landAllFallback
    static func landAllNotSentAddressRefused(_ reason: String) -> String {
        "LAND ALL was NOT sent: this phone's ground address is refused (\(GroundText.shorten(reason))). " + landAllFallback
    }
    static let landAllNotSentKeychainLocked =
        "LAND ALL was NOT sent: the Keychain is locked, so this phone cannot connect to the ground. " + landAllFallback
    static let landAllSendFailed = "LAND ALL was NOT sent: the connection failed while sending. " + landAllFallback
    static let landAllConnectionLost =
        "LAND ALL: the connection was lost before the ground answered. " + landAllFallback
    static let staleAfterDisconnect = "Last values: not connected to the ground."
    static let staleRefreshing = "Last values: waiting for a fresh fleet status from the ground."
    static let lastValueSuffix = " (last value)"
    static let partialRecord = "Partial record: only manual-control data so far; waiting for the fleet status."
    static let privacyNote = "Connects only to the ground station address you enter."

    /// The dashboard's partial-ack text (ui.js `landAllAckFeedback`): reached_any is true but at
    /// least one path reports 0 drones. Nil when no path reports 0.
    static func landAllPartial(_ ack: LandAllAck) -> String? {
        guard ack.reachedAny == true else { return nil }
        let paths: [(String, Int?)] = [("coordinator", ack.coordinator), ("transport", ack.transport), ("local", ack.local)]
        let none = paths.filter { $0.1 == 0 }.map(\.0)
        guard !none.isEmpty else { return nil }
        let got = paths.compactMap { name, count in count.flatMap { $0 > 0 ? "\(name) \($0)" : nil } }
        return "LAND ALL reached only some drones. Reached: " + got.joined(separator: ", ")
            + ". Reached none: " + none.joined(separator: ", ") + ". Land the rest by hand."
    }

    static let landAllNoAnswer =
        "LAND ALL: the ground never answered; land the drones with their own controls."
    static let earlierLandAllNoAnswer = "An earlier LAND ALL got no answer from the ground. Do not assume it landed the drones."
    static let staleNoRecentUpdate = "No recent update from the ground: no fleet status for 30 s."

    static func droneNotSent(_ rawId: String, action: String, linkOff: Bool, refusal: String? = nil) -> String {
        let id = GroundText.shortId(rawId)
        let why = refusal.map { "this phone's ground address is refused (\(GroundText.shorten($0)))" }
            ?? (linkOff ? "this phone's ground link is off or not set up" : "this phone is not connected to the ground")
        return "\(id): \(action) was NOT sent: \(why). Land it with its own controls, or use the ground laptop."
    }

    static func droneSendFailed(_ rawId: String, action: String) -> String {
        let id = GroundText.shortId(rawId)
        return "\(id): \(action) was NOT sent: the connection failed while sending. Land it with its own controls, or use the ground laptop."
    }

    static func droneNoUsableId(action: String) -> String {
        action == "Land"
            ? "Land was not sent: this drone has no usable id. Land it with its own controls."
            : "\(action) was not sent: this drone has no usable id."
    }

    static func droneSending(_ id: String, action: String) -> String { "\(GroundText.shortId(id)): \(action) sending\u{2026}" }

    static func droneHandedOver(_ id: String, action: String) -> String {
        "\(GroundText.shortId(id)): \(action) sending\u{2026} Handed to the connection; waiting for the ground's reply."
    }

    static func droneNoReplyYet(_ id: String, action: String) -> String {
        "\(GroundText.shortId(id)): \(action): no reply from the ground yet. Check the connection."
    }

    static func droneNoFinalAnswer(_ rawId: String, action: String) -> String {
        let id = GroundText.shortId(rawId)
        return action == "Land"
            ? "\(id): no final answer from the drone's edge. Land it with its own controls."
            : "\(id): \(action): no final answer from the drone's edge."
    }

    static func droneConnectionLost(_ rawId: String, action: String) -> String {
        let id = GroundText.shortId(rawId)
        return action == "Land"
            ? "\(id): connection lost before the answer; land it with its own controls."
            : "\(id): \(action): connection lost before the answer."
    }

    static func legacyLandAccepted(_ id: String) -> String {
        "\(GroundText.shortId(id)): Land accepted by the ground (no confirmation from the drone)."
    }

    /// A refused or failed manual reply whose action the phone did not send (or a land the
    /// phone did not send), for example an orphan auto-land that could not be delivered.
    static func droneActionFailed(_ id: String, action: String, why: String) -> String {
        "\(GroundText.shortId(id)): \(GroundText.shorten(action, max: 40)) failed (\(GroundText.shorten(why))). Land it with its own controls."
    }

    static func heldCountdown(_ seconds: Double) -> String {
        "HELD: auto-landing in \(GroundText.jsNumber(seconds)) s, no controller"
    }

    /// " (12 s ago)" for an age of at least one second, else "".
    static func age(_ seconds: TimeInterval) -> String {
        guard seconds.isFinite, seconds >= 1 else { return "" }
        if seconds < 120 { return " (\(Int(seconds)) s ago)" }
        return " (\(Int(seconds / 60)) min ago)"
    }
}

/// The client the view model talks to. The app uses `GroundClient`; tests use the same class
/// with a fake transport.
typealias GroundClientFactory = @MainActor (GroundEndpoint, GroundAPIKey) -> GroundClient

// MARK: - View model

@MainActor @Observable
final class GroundViewModel {
    /// The one instance the app uses (Ground tab and Settings section). Created on first use.
    static let live: GroundViewModel = {
        let model = GroundViewModel()
        model.startAppLifecycle()
        return model
    }()

    /// How long a command may wait for the ground's answer before "no reply yet" shows.
    static let replyTimeout: TimeInterval = 5
    /// How long the ground waits for an edge before it reports `no_reply` itself
    /// (`MANUAL_REPLY_TIMEOUT_S`). An accepted command with no final answer after
    /// `replyTimeout + manualReplyTimeout` is shown as a failure.
    static let manualReplyTimeout: TimeInterval = 4
    /// How long a success text stays. Failures and partial results stay until dismissed.
    static let successFadeAfter: TimeInterval = 10
    static let droneSuccessFadeAfter: TimeInterval = 60
    static let warningFadeAfter: TimeInterval = 8
    /// A connected link with no `fleet_status` for this long marks the state stale: the ground
    /// can keep answering pings and still have stopped sending state.
    static let freshnessTimeout: TimeInterval = 30
    /// The ground answers every LAND ALL within its own 35 s deadline. A tap still unanswered
    /// after this long will never be answered, so it stops counting: otherwise every later ack
    /// would be credited to the tap before it.
    static let landAllExpiresAfter: TimeInterval = 40
    static let maxBanners = 10
    static let maxToasts = 5
    static let maxDetections = 50
    static let maxMatchAlerts = 100
    static let maxFrameTiles = 16

    /// Every event the view model can emit. Equal to `GroundCommand.allowedEventNames`, which
    /// the package derives from the cases of `GroundCommand`, the contract's allow-list.
    static var emittableEventNames: Set<String> { GroundCommand.allowedEventNames }

    /// The client's emit policy: the contract's allow-list narrowed to what `GroundCommand`
    /// can build. A second fence behind the `GroundCommand` type.
    nonisolated static let emitPolicy = GroundEmitPolicy(narrowing: { GroundCommand.allowedEventNames.contains($0) })

    // MARK: Published state

    /// The single source of drone state. Render cards from `reducer.cards` (or `droneCards`).
    private(set) var reducer = DroneStateReducer()
    private(set) var linkStatus: GroundLinkStatus = .notConfigured
    /// True while the drone state is from before a disconnect, a reconnect or a gap in events.
    private(set) var isStateStale = false
    private(set) var staleNote: String?
    /// True when the link is connected but no `fleet_status` has arrived for `freshnessTimeout`.
    private(set) var hasNoRecentUpdate = false
    private(set) var groundStatus: StatusPayload?
    private(set) var landAllFeedback: GroundCommandFeedback?
    /// Keyed by drone id.
    private(set) var landFeedback: [String: GroundCommandFeedback] = [:]
    private(set) var resumeFeedback: [String: GroundCommandFeedback] = [:]
    /// Failures the ground reported for a drone outside the phone's own commands. Sticky.
    private(set) var droneAlerts: [String: GroundCommandFeedback] = [:]
    private(set) var banners: [GroundBanner] = []
    private(set) var toasts: [GroundToast] = []
    /// The time the countdowns, ages and fades are computed against; moved by `tick(now:)`.
    private(set) var now: Date

    // State for the views of later units (video tiles, plan grid, profiles, ceiling,
    // reasoning, match alerts). Filled from the event switch below.
    /// The latest `frame` per drone id (`""` for a frame without one), at most `maxFrameTiles`.
    /// Frames are decoded off the main actor.
    private(set) var latestFrames: [String: FramePayload] = [:]
    private(set) var latestFrameAt: [String: Date] = [:]
    private(set) var fleetPlan: FleetPlanPayload?
    // BEGIN plan-is-current (U9). The ground sends `fleet_plan` only when it has a plan, so a plan kept
    // from before a reconnect cannot be told from the current one. False from every connect, reconnect
    // and dropped-events gap until a `fleet_plan` arrives in the new session.
    private(set) var planIsCurrent = false
    // END plan-is-current
    private(set) var fleetDetections: [FleetDetection] = []
    private(set) var detectionProfiles: DetectionProfilesPayload?
    private(set) var ceilingSettings: CeilingSettingsPayload?
    private(set) var reasoningProvider: ReasoningProviderPayload?
    /// At most `maxMatchAlerts`, oldest dropped first.
    private(set) var matchAlerts: [MatchAlert] = []
    private(set) var streamVideoEnabled: Bool?

    // Settings, mirrored for the views.
    private(set) var savedAddress: String?
    private(set) var keyState: GroundKeyState
    private(set) var isEnabled: Bool

    var hasSavedKey: Bool { keyState == .saved }
    var isConfigured: Bool { savedAddress != nil && keyState == .saved }

    // MARK: Dependencies

    @ObservationIgnored private let keyStore: any GroundKeyStore
    @ObservationIgnored private let settings: GroundConnectionSettings
    @ObservationIgnored private let clientFactory: GroundClientFactory
    @ObservationIgnored private let http: any GroundHTTPClient
    @ObservationIgnored private let clock: () -> Date
    @ObservationIgnored private let logSink: GroundLogSink

    @ObservationIgnored private var client: GroundClient?
    @ObservationIgnored private var generation = 0
    /// Counts connection-state updates, so a late re-read cannot overwrite a newer state.
    @ObservationIgnored private var stateSequence = 0
    @ObservationIgnored private var attemptCounter = 0
    @ObservationIgnored private var appInBackground = false
    @ObservationIgnored private let lifetime = Lifetime()
    /// The value and arrival time of each drone's latest `orphan_land_in_s`.
    @ObservationIgnored private var orphanBase: [String: (value: Double, at: Date)] = [:]
    /// When the state became stale; countdowns freeze there.
    @ObservationIgnored private var staleSince: Date?
    /// LAND ALL taps handed to the connection and not yet answered, oldest first. Each
    /// `land_all_ack` answers the oldest.
    @ObservationIgnored private var landAllOutstanding: [(attempt: Int, sentAt: Date)] = []
    /// When the latest `fleet_status` arrived on the current connection; nil until one does.
    @ObservationIgnored private var lastFleetStatusAt: Date?
    /// Drones whose land was sent to a ground without a fleet, oldest first; each
    /// `command_ack` answers the oldest.
    @ObservationIgnored private var pendingLegacyLands: [String] = []

    init(
        keyStore: any GroundKeyStore = KeychainGroundKeyStore(),
        defaults: UserDefaults = .standard,
        http: any GroundHTTPClient = URLSessionGroundHTTPClient(),
        clock: @escaping () -> Date = Date.init,
        logSink: @escaping GroundLogSink = GroundLogging.unifiedLog,
        clientFactory: GroundClientFactory? = nil
    ) {
        self.keyStore = keyStore
        self.settings = GroundConnectionSettings(defaults: defaults)
        self.http = http
        self.clock = clock
        self.logSink = logSink
        self.clientFactory = clientFactory ?? { endpoint, key in
            GroundClient(
                configuration: GroundClient.Configuration(endpoint: endpoint, apiKey: key),
                emitPolicy: GroundViewModel.emitPolicy,
                http: http,
                logSink: logSink
            )
        }
        now = clock()
        savedAddress = settings.address
        keyState = keyStore.keyState
        isEnabled = settings.enabled
        linkStatus = idleStatus()
    }

    deinit {
        lifetime.end()
    }

    // MARK: Lifecycle

    /// Connects when the link is configured and switched on. Never starts a connection otherwise.
    func start() {
        connectIfWanted()
    }

    /// Observes the app moving to the background and back, starts the one-second ticker and
    /// connects if wanted. Used by `live`; tests drive the same handlers directly.
    func startAppLifecycle() {
        let center = NotificationCenter.default
        let background = center.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.appDidEnterBackground() }
        }
        let foreground = center.addObserver(forName: UIApplication.willEnterForegroundNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.appWillEnterForeground() }
        }
        lifetime.add(observer: background)
        lifetime.add(observer: foreground)
        let ticker = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self else { return }
                self.tick(now: self.clock())
            }
        }
        lifetime.add(task: ticker)
        appInBackground = UIApplication.shared.applicationState == .background
        connectIfWanted()
    }

    /// Closes the link gracefully (the client leaves the namespace and stops heartbeating) and
    /// marks the state stale. The phone cannot act on the drones from the background anyway.
    func appDidEnterBackground() {
        appInBackground = true
        guard client != nil else { return }
        log(.info, "app in background: closing the ground link")
        closeClient()
        markStale(GroundUIText.staleAfterDisconnect)
        linkStatus = .paused
    }

    /// Re-reads the Keychain (it may have been locked) and reconnects when the link is
    /// configured and switched on. State stays stale until a fresh `fleet_status` arrives.
    func appWillEnterForeground() {
        appInBackground = false
        keyState = keyStore.keyState
        if client == nil {
            linkStatus = idleStatus()
        }
        connectIfWanted()
    }

    // MARK: Settings actions

    /// Validates and saves the address. Reconnects to the new address when the link is on.
    /// Returns the error message, or nil on success.
    @discardableResult
    func saveAddress(_ text: String) -> String? {
        switch GroundAddress.validate(text) {
        case let .failure(error):
            return error.message
        case let .success(endpoint):
            let normalized = endpoint.description
            let changed = normalized != savedAddress
            settings.address = normalized
            savedAddress = normalized
            if changed { forgetGroundState() }
            restartIfWanted()
            return nil
        }
    }

    /// Saves the key in the Keychain. The caller clears its field; the key is never shown
    /// again. Returns an error message, or nil on success.
    @discardableResult
    func saveKey(_ text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "Enter the dashboard API key (the ground's FLIGHTRISK_API_KEY)." }
        do {
            try keyStore.saveKey(trimmed)
        } catch {
            return "The key could not be saved in the Keychain."
        }
        keyState = keyStore.keyState
        restartIfWanted()
        return nil
    }

    /// Deletes the saved key and closes the link.
    func removeKey() {
        keyStore.deleteKey()
        keyState = keyStore.keyState
        closeClient()
        markStale(GroundUIText.staleAfterDisconnect)
        linkStatus = idleStatus()
    }

    /// Switches the link on or off. On connects only when an address and a key are saved.
    func setEnabled(_ enabled: Bool) {
        settings.enabled = enabled
        isEnabled = enabled
        if enabled {
            connectIfWanted()
            if client == nil { linkStatus = idleStatus() }
        } else {
            if client != nil {
                closeClient()
                markStale(GroundUIText.staleAfterDisconnect)
            }
            linkStatus = idleStatus()
        }
    }

    /// "Test connection": the REST health probe with the saved address and key. The text never
    /// contains the key.
    func testConnection() async -> String {
        if let reason = addressRefusal { return "Refused: \(GroundText.shorten(reason)). Nothing was sent." }
        guard let address = savedAddress, case let .success(endpoint) = GroundAddress.validate(address) else {
            return "Save the ground station address first."
        }
        let key: GroundAPIKey
        switch keyStore.lookupKey() {
        case let .found(found): key = found
        case .notFound: return "Save the dashboard API key first."
        case .unavailable: return "Keychain locked: unlock the phone and try again."
        }
        let result = await GroundHealthProbe(endpoint: endpoint, apiKey: key, http: http).run()
        switch result {
        case let .ok(version):
            return "Reachable, key accepted" + (version.map { " (ground version \(GroundText.shorten($0, max: GroundText.maxIdText)))" } ?? "") + "."
        case let .wrongKey(version):
            return "Reachable, but the ground refused the key" + (version.map { " (ground version \(GroundText.shorten($0, max: GroundText.maxIdText)))" } ?? "") + "."
        case let .unreachable(detail):
            return "Unreachable: \(GroundText.shorten(detail)). Check the address and that the phone is on the ground's network."
        case let .addressNotAllowed(reason):
            return "Refused: \(GroundText.shorten(reason)). Nothing was sent."
        case let .unexpectedStatus(status):
            return "The address answered with HTTP \(status); it may not be a FlightRisk ground station."
        case let .invalidResponse(detail):
            return "Not a FlightRisk ground station response: \(GroundText.shorten(detail))."
        }
    }

    /// The banner's "Retry": ends a wrong-key or backoff wait at once.
    func retryConnection() {
        if let client {
            Task { await client.retryNow() }
        } else {
            keyState = keyStore.keyState
            connectIfWanted()
            if client == nil { linkStatus = idleStatus() }
        }
    }

    /// Requests the full state again (pull to refresh).
    func refresh() async {
        guard client != nil else { return }
        await sendStateRequests()
    }

    // MARK: Commands

    /// LAND ALL. Never disabled, never confirmed, never queued. Always leaves a visible result.
    func landAll() async {
        let attempt = nextAttempt()
        let started = clock()
        landAllFeedback = GroundCommandFeedback(attempt: attempt, phase: .sending, text: GroundText.landAllSending, startedAt: started, updatedAt: started)
        let generationAtSend = generation
        let outcome = await emit(.landAll)
        let at = clock()
        if outcome == .sent, generation == generationAtSend, linkStatus.isConnected {
            // Only a send on the link that is still up can be answered by an ack.
            landAllOutstanding.append((attempt, at))
        }
        guard landAllFeedback?.attempt == attempt else { return }  // A newer tap took over.
        switch outcome {
        case .sent:
            if landAllFeedback?.phase == .sending {
                landAllFeedback?.phase = .handedOver
                landAllFeedback?.text = GroundUIText.landAllHandedOver
                landAllFeedback?.updatedAt = at
            }
        case .notConnected:
            let text = linkStatus == .wrongKey ? GroundUIText.landAllNotSentWrongKey : GroundText.landAllNotSent
            landAllFeedback = failure(text, attempt: attempt, startedAt: started)
        case .noClient, .shutDown:
            let text: String
            if linkStatus == .keychainLocked {
                text = GroundUIText.landAllNotSentKeychainLocked
            } else if case let .addressRefused(reason) = linkStatus {
                text = GroundUIText.landAllNotSentAddressRefused(reason)
            } else {
                text = GroundUIText.landAllNotSentLinkOff
            }
            landAllFeedback = failure(text, attempt: attempt, startedAt: started)
        case .sendFailed, .refused:
            landAllFeedback = failure(GroundUIText.landAllSendFailed, attempt: attempt, startedAt: started)
        }
    }

    /// Lands one drone (`drone_command {command: "land"}`). Always enabled while the card exists,
    /// whatever the drone's link or control state. An id that cannot be sent is refused with a
    /// visible text.
    func landDrone(_ id: String) async {
        await droneCommand(.landDrone(droneId: id), id: id, action: "Land")
    }

    /// Resumes the search of a held drone nobody controls (`manual_resume_search`). Sent only
    /// when `canResume(id)` is true.
    func resumeHeldDrone(_ id: String) async {
        let command = GroundCommand.resumeHeldDrone(droneId: id)
        guard command.isSendable else {
            refuseUnusableId(id, isLand: false, action: "Resume search")
            return
        }
        guard canResume(id) else {
            log(.warning, "resume not offered for this drone; nothing sent")
            return
        }
        await droneCommand(command, id: id, action: "Resume search")
    }

    /// Whether to show "Resume search": the reducer says the drone is held and nobody controls
    /// it, the drone's own link is up (the ground refuses a resume for a DOWN or LOST drone),
    /// the state is live, and the link is up.
    func canResume(_ id: String) -> Bool {
        guard reducer.canResume(id), !isStateStale, linkStatus.isConnected else { return false }
        return reducer.card(for: id)?.link.isStale == false
    }

    func dismissLandAllFeedback() { landAllFeedback = nil }
    func dismissBanner(_ id: UUID) { banners.removeAll { $0.id == id } }
    func dismissToast(_ id: UUID) { toasts.removeAll { $0.id == id } }
    func dismissDroneAlert(_ id: String) { droneAlerts[id] = nil }

    // MARK: Derived view state

    var droneCards: [GroundDroneCardModel] {
        let stale = isStateStale
        func lastValue(_ text: String) -> String {
            stale && text != "--" && !text.isEmpty ? text + GroundUIText.lastValueSuffix : text
        }
        return reducer.cards.map { card in
            let id = card.id
            var controlText = card.control.text
            if card.control.orphanLandInS != nil {
                controlText = GroundUIText.heldCountdown(countdownSeconds(for: id, card: card))
            }
            let chip: String?
            switch card.control {
            case .held: chip = "HELD"
            case .manual: chip = "MANUAL"
            case .autonomous: chip = nil
            }
            var note: String?
            if stale { note = staleNote ?? GroundUIText.staleAfterDisconnect }
            else if let s = card.staleNote { note = s }
            let land = landFeedback[id]
            let resume = resumeFeedback[id]
            let alert = droneAlerts[id]
            return GroundDroneCardModel(
                card: card,
                linkChipText: stale ? "LAST: \(card.link.text)" : card.link.text,
                linkChipGreyed: stale,
                controlChip: chip,
                controlText: lastValue(controlText),
                batteryText: lastValue(card.batteryText),
                altitudeText: lastValue(card.altitudeText),
                isPartial: reducer.isPartial(id),
                isStateStale: stale,
                isStale: stale || card.link.isStale,
                staleNote: note,
                canResume: canResume(id),
                landFeedback: land,
                resumeFeedback: resume,
                alert: alert,
                landFeedbackText: land.map { feedbackText($0) },
                resumeFeedbackText: resume.map { feedbackText($0) },
                alertText: alert.map { feedbackText($0) }
            )
        }
    }

    /// "2 drones, 1 connected", with "(last values)" while stale.
    var readoutText: String {
        let cards = reducer.cards
        let connected = cards.filter { $0.link == .connected }.count
        let noun = cards.count == 1 ? "drone" : "drones"
        var text = "\(cards.count) \(noun), \(connected) connected"
        if reducer.isLegacyShape { text += " (no fleet: local drone only)" }
        if isStateStale { text += " (last values)" }
        return text
    }

    /// A feedback text with its age once it is no longer in flight.
    func feedbackText(_ f: GroundCommandFeedback) -> String {
        switch f.phase {
        case .sending, .handedOver: return f.text
        default: return f.text + GroundUIText.age(now.timeIntervalSince(f.updatedAt))
        }
    }

    /// The HELD countdown, counted down locally from the latest server value as the contract
    /// says, and frozen while the state is stale.
    private func countdownSeconds(for id: String, card: DroneCardState) -> Double {
        let received = card.control.orphanLandInS ?? 0
        guard let base = orphanBase[id], base.value == received else { return max(0, received) }
        let reference = staleSince ?? now
        return OrphanCountdown.remaining(received: base.value, elapsedSeconds: reference.timeIntervalSince(base.at))
    }

    // MARK: Ticking

    /// Moves `now` (countdowns, ages), turns unanswered commands into "no reply yet" after
    /// `replyTimeout` and accepted commands without a final answer into failures after
    /// `replyTimeout + manualReplyTimeout`, and fades success texts and warnings. Failures and
    /// partial results never fade.
    func tick(now date: Date) {
        now = date
        if var f = landAllFeedback {
            if (f.phase == .sending || f.phase == .handedOver), date.timeIntervalSince(f.startedAt) >= Self.replyTimeout {
                f.phase = .noReplyYet
                f.text = GroundText.landAllNoReplyYet
                f.updatedAt = date
                landAllFeedback = f
            } else if f.phase == .succeeded, date.timeIntervalSince(f.updatedAt) >= Self.successFadeAfter {
                landAllFeedback = nil
            }
        }
        expireLandAll(at: date)
        checkFreshness(at: date)
        let lands = Self.aged(landFeedback, action: "Land", at: date)
        if lands != landFeedback { landFeedback = lands }
        let resumes = Self.aged(resumeFeedback, action: "Resume search", at: date)
        if resumes != resumeFeedback { resumeFeedback = resumes }
        if toasts.contains(where: { !$0.isError && date.timeIntervalSince($0.receivedAt) >= Self.warningFadeAfter }) {
            toasts.removeAll { !$0.isError && date.timeIntervalSince($0.receivedAt) >= Self.warningFadeAfter }
        }
    }

    /// A LAND ALL tap unanswered for `landAllExpiresAfter` stops waiting for an ack: its own
    /// feedback becomes a failure (or a banner when a newer tap took over the feedback), and a
    /// later ack is credited to the newer tap. Nothing is resent.
    private func expireLandAll(at date: Date) {
        let expired = landAllOutstanding.filter { date.timeIntervalSince($0.sentAt) >= Self.landAllExpiresAfter }
        guard !expired.isEmpty else { return }
        landAllOutstanding.removeAll { entry in expired.contains { $0.attempt == entry.attempt } }
        for entry in expired {
            if var current = landAllFeedback, current.attempt == entry.attempt, current.isWaiting {
                current.phase = .failed
                current.text = GroundUIText.landAllNoAnswer
                current.updatedAt = date
                landAllFeedback = current
            } else {
                addBanner(GroundUIText.earlierLandAllNoAnswer, isFailure: true, at: date)
            }
        }
    }

    /// The ground keeps answering pings but stopped sending `fleet_status`: the values are no
    /// longer live. The next `fleet_status` clears it.
    private func checkFreshness(at date: Date) {
        guard linkStatus.isConnected, !isStateStale, let last = lastFleetStatusAt,
              date.timeIntervalSince(last) >= Self.freshnessTimeout else { return }
        markStale(GroundUIText.staleNoRecentUpdate, noRecentUpdate: true)
    }

    /// The per-drone feedback after time has passed. Returns the same dictionary when nothing
    /// changed, so the caller's assignment does not need to publish.
    private static func aged(_ feedback: [String: GroundCommandFeedback], action: String, at date: Date) -> [String: GroundCommandFeedback] {
        var result = feedback
        for (id, f) in feedback {
            let sinceStart = date.timeIntervalSince(f.startedAt)
            let sinceUpdate = date.timeIntervalSince(f.updatedAt)
            if (f.phase == .sending || f.phase == .handedOver), sinceStart >= replyTimeout {
                var copy = f
                copy.phase = .noReplyYet
                copy.text = GroundUIText.droneNoReplyYet(id, action: action)
                copy.updatedAt = date
                result[id] = copy
            } else if f.phase == .accepted, f.awaitsFinalAnswer, sinceUpdate >= replyTimeout + manualReplyTimeout {
                var copy = f
                copy.phase = .failed
                copy.text = GroundUIText.droneNoFinalAnswer(id, action: action)
                copy.updatedAt = date
                result[id] = copy
            } else if f.phase == .succeeded, sinceUpdate >= droneSuccessFadeAfter {
                result[id] = nil
            }
        }
        return result
    }

    // MARK: Event handling

    /// Applies one client event. Every server event goes through here in arrival order.
    func handle(_ event: SocketIOMini.GroundEvent) {
        switch event {
        case let .stateChanged(state):
            stateSequence += 1
            apply(state)
        case .reconnected:
            planIsCurrent = false
            markStale(GroundUIText.staleRefreshing)
            Task { await self.sendStateRequests() }
        case let .eventsDropped(count):
            log(.warning, "\(count) events were dropped; requesting the full state again")
            planIsCurrent = false
            markStale(GroundUIText.staleRefreshing)
            let sequence = stateSequence
            let generationAtGap = generation
            Task {
                if let client = self.client {
                    let state = await client.state
                    // A state change that arrived meanwhile is newer than this re-read.
                    if self.generation == generationAtGap, self.stateSequence == sequence { self.apply(state) }
                }
                await self.sendStateRequests()
            }
        case let .server(serverEvent):
            guard let payload = serverEvent.payload else { return }
            handle(GroundProtocol.GroundEvent.decode(name: serverEvent.name, payload: payload))
        }
    }

    /// Applies one decoded server event.
    func handle(_ incoming: GroundProtocol.GroundEvent) {
        // Every string from the ground is cut here, before anything is stored or composed.
        let event = GroundIngest.sanitized(incoming)
        let at = clock()
        switch event {
        case let .status(p):
            groundStatus = p
        case let .fleetStatus(p):
            reducer.apply(event)
            if p.drones != nil {
                orphanBase = [:]
                for (id, record) in p.drones ?? [:] {
                    if let v = record.orphanLandInS, v.isFinite { orphanBase[id] = (v, at) }
                }
                if linkStatus.isConnected {
                    clearStale()
                    lastFleetStatusAt = at
                }
            }
        case let .manualStatus(p):
            reducer.apply(event)
            for id in (p.drones ?? [:]).keys {
                if let v = reducer.drones[id]?.orphanLandInS, v.isFinite { orphanBase[id] = (v, at) } else { orphanBase[id] = nil }
            }
        case let .fleetEvent(ev):
            if let info = GroundText.fleetEventBannerInfo(ev) {
                addBanner(info.text, isFailure: info.isFailure, at: at)
                if info.isFailure, ev.resolvedKind == .landAll {
                    // A failed LAND ALL stays in the top bar until dismissed or a new tap.
                    landAllFeedback = GroundCommandFeedback(
                        attempt: landAllFeedback?.attempt ?? nextAttempt(), phase: .failed, text: info.text,
                        startedAt: landAllFeedback?.startedAt ?? at, updatedAt: at
                    )
                }
            }
            if let refusal = GroundText.fleetRefusal(ev) {
                addBanner(refusal, isFailure: true, at: at)
            }
        case let .fleetPlan(p):
            fleetPlan = p
            planIsCurrent = true
        case let .fleetDetection(d):
            fleetDetections.append(d)
            if fleetDetections.count > Self.maxDetections { fleetDetections.removeFirst(fleetDetections.count - Self.maxDetections) }
        case let .frame(f):
            storeFrame(f, at: at)
        case let .matchAlert(a), let .matchAlertReminder(a):
            if let id = a.alertId, let index = matchAlerts.firstIndex(where: { $0.alertId == id }) {
                matchAlerts[index] = a
            } else {
                matchAlerts.append(a)
                if matchAlerts.count > Self.maxMatchAlerts { matchAlerts.removeFirst(matchAlerts.count - Self.maxMatchAlerts) }
            }
        case let .detectionProfiles(p):
            detectionProfiles = p
        case let .ceilingSettings(p):
            ceilingSettings = p
        case let .reasoningProvider(p):
            reasoningProvider = p
        case let .manualAck(reply):
            applyManualReply(reply, final: false, at: at)
        case let .manualResult(reply):
            applyManualReply(reply, final: true, at: at)
        case let .landAllAck(ack):
            applyLandAllAck(ack, at: at)
        case .commandAck:
            guard !pendingLegacyLands.isEmpty else { break }
            let id = pendingLegacyLands.removeFirst()
            if let current = landFeedback[id], current.isWaiting {
                var accepted = current
                accepted.phase = .accepted
                accepted.awaitsFinalAnswer = false  // A ground without a fleet never sends one.
                accepted.text = GroundUIText.legacyLandAccepted(id)
                accepted.updatedAt = at
                landFeedback[id] = accepted
            }
        case let .streamVideoState(s):
            streamVideoEnabled = s.enabled
        case let .error(e):
            addToast(e.message.map { GroundText.shorten($0) } ?? "The ground reported an error.", isError: true, at: at)
        case let .warning(e):
            addToast(e.message.map { GroundText.shorten($0) } ?? "The ground reported a warning.", isError: false, at: at)
        case .unknown, .undecodable:
            break
        }
    }

    // MARK: Private: connection

    /// The refusal sentence when the saved address is a cleartext address off the local network.
    private var addressRefusal: String? {
        guard let address = savedAddress, case let .failure(.refused(reason, _)) = GroundAddress.validate(address) else { return nil }
        return reason
    }

    private func idleStatus() -> GroundLinkStatus {
        if let reason = addressRefusal { return .addressRefused(reason) }
        if savedAddress != nil, keyState == .unavailable { return .keychainLocked }
        if !isConfigured { return .notConfigured }
        if !isEnabled { return .off }
        if appInBackground { return .paused }
        return .stopped
    }

    private func connectIfWanted() {
        guard client == nil, isEnabled, !appInBackground else { return }
        guard let address = savedAddress, case let .success(endpoint) = GroundAddress.validate(address) else {
            linkStatus = addressRefusal.map { .addressRefused($0) } ?? .notConfigured
            return
        }
        // The key goes from the Keychain straight into the client and is not kept here.
        let key: GroundAPIKey
        switch keyStore.lookupKey() {
        case let .found(found):
            key = found
            keyState = .saved
        case .notFound:
            keyState = .none
            linkStatus = .notConfigured
            return
        case .unavailable:
            keyState = .unavailable
            linkStatus = .keychainLocked
            return
        }
        generation += 1
        let current = generation
        let newClient = clientFactory(endpoint, key)
        client = newClient
        lifetime.setClient(newClient)
        linkStatus = .connecting(attempt: 1)
        log(.info, "connecting to \(endpoint)")
        // `events` and `frames` are each read from exactly this one place.
        let events = newClient.events
        let frames = newClient.frames
        let eventTask = Task { [weak self] in
            for await event in events {
                guard let self else { return }
                guard self.generation == current else { return }
                self.handle(event)
            }
        }
        // Frames are decoded off the main actor; only the decoded payload hops over.
        let clock = self.clock
        let frameTask = Task.detached { [weak self] in
            for await frame in frames {
                guard let payload = frame.payload,
                      case let .frame(decoded) = GroundProtocol.GroundEvent.decode(name: frame.name, payload: payload)
                else { continue }
                let at = clock()
                guard let self else { return }
                await self.receiveFrame(decoded, at: at, generation: current)
            }
        }
        lifetime.replaceStreamTasks([eventTask, frameTask])
        Task { await newClient.connect() }
    }

    private func receiveFrame(_ frame: FramePayload, at: Date, generation frameGeneration: Int) {
        guard frameGeneration == generation else { return }
        storeFrame(frame, at: at)
    }

    private func closeClient() {
        guard let old = client else { return }
        client = nil
        generation += 1
        lifetime.setClient(nil)
        lifetime.replaceStreamTasks([])
        connectionLost()
        Task { await old.shutdown() }
    }

    private func restartIfWanted() {
        if client != nil {
            closeClient()
            markStale(GroundUIText.staleAfterDisconnect)
        }
        connectIfWanted()
        if client == nil { linkStatus = idleStatus() }
    }

    /// A different ground: nothing from the old one may stay on screen.
    private func forgetGroundState() {
        reducer.reset()
        orphanBase = [:]
        groundStatus = nil
        latestFrames = [:]
        latestFrameAt = [:]
        fleetPlan = nil
        planIsCurrent = false
        fleetDetections = []
        detectionProfiles = nil
        ceilingSettings = nil
        reasoningProvider = nil
        matchAlerts = []
        streamVideoEnabled = nil
        landFeedback = [:]
        resumeFeedback = [:]
        droneAlerts = [:]
        pendingLegacyLands = []
        landAllOutstanding = []
        lastFleetStatusAt = nil
    }

    private func apply(_ state: GroundConnectionState) {
        switch state {
        case .idle:
            break
        case let .connecting(attempt):
            linkStatus = .connecting(attempt: attempt)
            markStale(GroundUIText.staleAfterDisconnect)
            connectionLost()
        case .connected:
            linkStatus = .connected
        case let .reconnecting(attempt, retryIn, reason):
            let seconds = Double(retryIn.components.seconds) + Double(retryIn.components.attoseconds) / 1e18
            linkStatus = .reconnecting(attempt: attempt, retryInSeconds: seconds, reason: GroundText.shorten(reason.description))
            markStale(GroundUIText.staleAfterDisconnect)
            connectionLost()
        case .failed:
            linkStatus = .wrongKey
            markStale(GroundUIText.staleAfterDisconnect)
            connectionLost()
        case .stopped:
            linkStatus = idleStatus()
            markStale(GroundUIText.staleAfterDisconnect)
            connectionLost()
        }
    }

    /// The link is gone: no answer to anything sent on it can arrive any more. Every command
    /// still waiting becomes a visible failure; nothing is resent.
    private func connectionLost() {
        planIsCurrent = false
        let at = clock()
        landAllOutstanding = []
        pendingLegacyLands = []
        if var f = landAllFeedback, f.phase == .sending || f.phase == .handedOver || f.phase == .noReplyYet {
            f.phase = .failed
            f.text = GroundUIText.landAllConnectionLost
            f.updatedAt = at
            landAllFeedback = f
        }
        func lose(_ feedback: [String: GroundCommandFeedback], action: String) -> [String: GroundCommandFeedback] {
            var result = feedback
            for (id, f) in feedback where f.isWaiting && f.phase != .sending {
                var copy = f
                copy.phase = .failed
                copy.text = GroundUIText.droneConnectionLost(id, action: action)
                copy.updatedAt = at
                result[id] = copy
            }
            return result
        }
        let lostLands = lose(landFeedback, action: "Land")
        if lostLands != landFeedback { landFeedback = lostLands }
        let lostResumes = lose(resumeFeedback, action: "Resume search")
        if lostResumes != resumeFeedback { resumeFeedback = lostResumes }
    }

    private func markStale(_ note: String, noRecentUpdate: Bool = false) {
        if !isStateStale { staleSince = clock() }
        isStateStale = true
        staleNote = note
        hasNoRecentUpdate = noRecentUpdate
    }

    private func clearStale() {
        hasNoRecentUpdate = false
        isStateStale = false
        staleNote = nil
        staleSince = nil
    }

    private func sendStateRequests() async {
        for command in GroundCommand.stateRequests {
            _ = await emit(command)
        }
    }

    // MARK: Private: emitting

    private enum EmitOutcome: Equatable {
        case sent, notConnected, sendFailed, shutDown, noClient, refused
    }

    /// The one path to the wire. Only a `GroundCommand` can be sent, only through
    /// `wireMessage`, and an unsendable command (an empty drone id) is refused here.
    private func emit(_ command: GroundCommand) async -> EmitOutcome {
        guard let wire = command.wireMessage else {
            log(.warning, "refused to send \(command.eventName): the drone id is empty")
            return .refused
        }
        guard let client else { return .noClient }
        do {
            switch try await client.emit(wire.eventName, payload: wire.payloadJSON) {
            case .sent: return .sent
            case .notSent(.notConnected): return .notConnected
            case .notSent(.sendFailed): return .sendFailed
            case .notSent(.shutDown): return .shutDown
            }
        } catch {
            log(.error, "\(wire.eventName) was refused by the client")
            return .refused
        }
    }

    private func nextAttempt() -> Int {
        attemptCounter += 1
        return attemptCounter
    }

    private func refuseUnusableId(_ id: String, isLand: Bool, action: String) {
        log(.warning, "refused \(action) for a drone without a usable id")
        let at = clock()
        setDroneFeedback(isLand, id, GroundCommandFeedback(
            attempt: nextAttempt(), phase: .failed, text: GroundUIText.droneNoUsableId(action: action), startedAt: at, updatedAt: at
        ))
    }

    private func droneCommand(_ command: GroundCommand, id: String, action: String) async {
        let isLand: Bool
        if case .landDrone = command { isLand = true } else { isLand = false }
        guard command.isSendable else {
            refuseUnusableId(id, isLand: isLand, action: action)
            return
        }
        let attempt = nextAttempt()
        let started = clock()
        setDroneFeedback(isLand, id, GroundCommandFeedback(attempt: attempt, phase: .sending, text: GroundUIText.droneSending(id, action: action), startedAt: started, updatedAt: started))
        let generationAtSend = generation
        let outcome = await emit(command)
        let at = clock()
        if outcome == .sent, isLand, reducer.isLegacyShape, generation == generationAtSend, linkStatus.isConnected {
            pendingLegacyLands.append(id)
        }
        guard droneFeedback(isLand, id)?.attempt == attempt else { return }
        switch outcome {
        case .sent:
            if droneFeedback(isLand, id)?.phase == .sending {
                setDroneFeedback(isLand, id, GroundCommandFeedback(attempt: attempt, phase: .handedOver, text: GroundUIText.droneHandedOver(id, action: action), startedAt: started, updatedAt: at))
            }
        case .notConnected:
            setDroneFeedback(isLand, id, failure(GroundUIText.droneNotSent(id, action: action, linkOff: false), attempt: attempt, startedAt: started))
        case .noClient, .shutDown:
            setDroneFeedback(isLand, id, failure(GroundUIText.droneNotSent(id, action: action, linkOff: true, refusal: addressRefusal), attempt: attempt, startedAt: started))
        case .sendFailed, .refused:
            setDroneFeedback(isLand, id, failure(GroundUIText.droneSendFailed(id, action: action), attempt: attempt, startedAt: started))
        }
    }

    private func droneFeedback(_ isLand: Bool, _ id: String) -> GroundCommandFeedback? {
        isLand ? landFeedback[id] : resumeFeedback[id]
    }

    private func setDroneFeedback(_ isLand: Bool, _ id: String, _ value: GroundCommandFeedback) {
        if isLand { landFeedback[id] = value } else { resumeFeedback[id] = value }
    }

    private func failure(_ text: String, attempt: Int, startedAt: Date) -> GroundCommandFeedback {
        GroundCommandFeedback(attempt: attempt, phase: .failed, text: text, startedAt: startedAt, updatedAt: clock())
    }

    // MARK: Private: answers

    /// Each ack answers the oldest outstanding LAND ALL. While a newer tap still waits, an
    /// earlier tap's result is shown as a detail (and as a banner when it is not a success),
    /// and the newer tap keeps its own "sending" and "no reply yet" states.
    private func applyLandAllAck(_ ack: LandAllAck, at: Date) {
        let phase: GroundCommandFeedback.Phase
        let text: String
        let base = GroundText.landAllResult(ack)
        if base.isFailure {
            phase = .failed
            text = base.text
        } else if let partial = GroundUIText.landAllPartial(ack) {
            phase = .partial
            text = partial
        } else {
            phase = .succeeded
            text = base.text
        }
        if !landAllOutstanding.isEmpty { landAllOutstanding.removeFirst() }
        let latestStillWaiting = !landAllOutstanding.isEmpty
        if latestStillWaiting, landAllFeedback?.isWaiting == true {
            landAllFeedback?.detail = "Earlier LAND ALL: " + text
            if phase != .succeeded { addBanner(text, isFailure: true, at: at) }
            return
        }
        if landAllFeedback?.phase == .failed {
            // A failure shown for LAND ALL stays visible; this answer is added to it.
            landAllFeedback?.detail = text
            landAllFeedback?.updatedAt = at
            if phase != .succeeded { addBanner(text, isFailure: true, at: at) }
            return
        }
        landAllFeedback = GroundCommandFeedback(
            attempt: landAllFeedback?.attempt ?? nextAttempt(), phase: phase, text: text,
            startedAt: landAllFeedback?.startedAt ?? at, updatedAt: at
        )
    }

    private func applyManualReply(_ received: ManualReply, final: Bool, at: Date) {
        let reply = GroundIngest.sanitized(received)
        let id = reply.droneId ?? ""
        let shownId = GroundText.shortId(id)
        let actionWire = reply.action?.wire ?? "command"
        let isLand: Bool?
        switch reply.action {
        case .land?: isLand = true
        case .resumeSearch?: isLand = false
        default: isLand = nil
        }
        let ownCommand = isLand.flatMap { droneFeedback($0, id) } != nil

        if reply.ok == false {
            var why = reply.reason.map { GroundText.nackReasonText($0) } ?? "refused"
            if let d = reply.detail, !d.isEmpty { why = d }
            if let isLand, ownCommand {
                // An answer to the phone's own land or resume.
                var shown = reply
                shown.droneId = reply.droneId.map { GroundText.shortId($0) }
                let refusal = GroundText.manualRefusal(shown) ?? "\(shownId): \(why)"
                let text = isLand ? refusal + ". Land the drone with its own controls." : refusal + "."
                let started = droneFeedback(isLand, id)?.startedAt ?? at
                setDroneFeedback(isLand, id, GroundCommandFeedback(attempt: nextAttempt(), phase: .failed, text: text, startedAt: started, updatedAt: at))
            } else {
                // Any other failure the ground reports for a drone, for example an orphan
                // auto-land that could not be delivered. Sticky on the card until dismissed.
                let who = id.isEmpty ? "drone" : id
                let text = GroundUIText.droneActionFailed(who, action: actionWire, why: why)
                droneAlerts[who] = GroundCommandFeedback(attempt: nextAttempt(), phase: .failed, text: text, startedAt: at, updatedAt: at)
                if reducer.drones[who] == nil { addBanner(text, isFailure: true, at: at) }
            }
            return
        }
        guard reply.ok == true, let isLand, !id.isEmpty else { return }
        let started = droneFeedback(isLand, id)?.startedAt ?? at
        let attempt = droneFeedback(isLand, id)?.attempt ?? nextAttempt()
        if final {
            let text = isLand ? "\(shownId): Land acknowledged by the drone's edge." : "\(shownId): search resumed."
            setDroneFeedback(isLand, id, GroundCommandFeedback(attempt: attempt, phase: .succeeded, text: text, startedAt: started, updatedAt: at))
        } else if droneFeedback(isLand, id)?.phase != .succeeded {
            let action = isLand ? "Land" : "Resume search"
            let text = "\(shownId): \(action) accepted by the ground; waiting for the drone's edge."
            setDroneFeedback(isLand, id, GroundCommandFeedback(attempt: attempt, phase: .accepted, text: text, startedAt: started, updatedAt: at))
        }
    }

    private func storeFrame(_ frame: FramePayload, at: Date) {
        let id = frame.droneId ?? ""
        latestFrames[id] = frame
        latestFrameAt[id] = at
        if latestFrames.count > Self.maxFrameTiles,
           let oldest = latestFrameAt.min(by: { $0.value < $1.value })?.key {
            latestFrames[oldest] = nil
            latestFrameAt[oldest] = nil
        }
    }

    private func addBanner(_ text: String, isFailure: Bool, at: Date) {
        banners.insert(GroundBanner(id: UUID(), text: text, isFailure: isFailure, receivedAt: at), at: 0)
        if banners.count > Self.maxBanners {
            // Drop the oldest non-failure first; failures go only when nothing else is left.
            if let index = banners.lastIndex(where: { !$0.isFailure }) { banners.remove(at: index) } else { banners.removeLast() }
        }
    }

    private func addToast(_ text: String, isError: Bool, at: Date) {
        toasts.insert(GroundToast(id: UUID(), text: text, isError: isError, receivedAt: at), at: 0)
        while toasts.count > Self.maxToasts {
            // Drop the oldest warning first; errors go only when nothing else is left.
            if let index = toasts.lastIndex(where: { !$0.isError }) { toasts.remove(at: index) } else { toasts.removeLast() }
        }
    }

    private func log(_ level: GroundLogLevel, _ message: String) {
        logSink(level, "GroundViewModel: " + message)
    }
}

/// What `deinit` needs: the client and the tasks, reachable without the main actor.
private final class Lifetime: @unchecked Sendable {
    private let lock = NSLock()
    private var client: GroundClient?
    private var streamTasks: [Task<Void, Never>] = []
    private var otherTasks: [Task<Void, Never>] = []
    private var observers: [NSObjectProtocol] = []

    func setClient(_ client: GroundClient?) { lock.withLock { self.client = client } }

    func replaceStreamTasks(_ tasks: [Task<Void, Never>]) {
        let old: [Task<Void, Never>] = lock.withLock {
            let previous = streamTasks
            streamTasks = tasks
            return previous
        }
        old.forEach { $0.cancel() }
    }

    func add(task: Task<Void, Never>) { lock.withLock { otherTasks.append(task) } }
    func add(observer: NSObjectProtocol) { lock.withLock { observers.append(observer) } }

    /// Shuts the client down and stops everything. Called from the view model's `deinit`.
    func end() {
        let (client, tasks, observers): (GroundClient?, [Task<Void, Never>], [NSObjectProtocol]) = lock.withLock {
            let result = (self.client, streamTasks + otherTasks, self.observers)
            self.client = nil
            streamTasks = []
            otherTasks = []
            self.observers = []
            return result
        }
        tasks.forEach { $0.cancel() }
        observers.forEach { NotificationCenter.default.removeObserver($0) }
        if let client {
            Task { await client.shutdown() }
        }
    }
}
