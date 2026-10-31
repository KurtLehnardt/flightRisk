import Foundation
import GroundProtocol
import os
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
//   answer arrives; a missing answer shows "no reply yet" after `replyTimeout`.
// - No command is retried automatically.
// - Drone state comes only from `DroneStateReducer`, fed with every server event in arrival
//   order. State from before a disconnect is kept but marked stale until a fresh
//   `fleet_status` arrives.
// - The API key goes from the Keychain straight into `GroundAPIKey` and from there only into
//   the client. It is never stored in this object, shown, or logged.

// MARK: - Settings storage

/// Where the dashboard API key is kept. Only the Keychain implementation is used in the app.
protocol GroundKeyStore: Sendable {
    /// Whether a key is saved, without handing it out.
    var hasKey: Bool { get }
    /// The saved key wrapped for the client, or nil when none is saved.
    func loadKey() -> GroundAPIKey?
    func saveKey(_ key: String) throws
    func deleteKey()
}

/// The dashboard API key in the Keychain, through `KeychainHelper` with provider `ground`.
///
/// The contract names this secret `FLIGHTRISK_DASHBOARD_API_KEY` (the ground's
/// `FLIGHTRISK_API_KEY`). `KeychainHelper` names its items `flightrisk_api_key_<provider>`,
/// so the item's account is `flightrisk_api_key_ground` in the service `com.flightrisk.app`,
/// stored with `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`.
struct KeychainGroundKeyStore: GroundKeyStore {
    static let provider = "ground"
    static let contractSecretName = "FLIGHTRISK_DASHBOARD_API_KEY"

    var hasKey: Bool { loadKey() != nil }

    func loadKey() -> GroundAPIKey? {
        guard let stored = KeychainHelper.loadApiKey(provider: Self.provider), !stored.isEmpty else { return nil }
        return GroundAPIKey(stored)
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
            return .failure(.invalid(error.description))
        } catch {
            return .failure(.invalid("\"\(trimmed)\" is not a ground station address of the form ws://HOST:PORT."))
        }
    }
}

enum GroundAddressError: Error, Equatable {
    case empty
    case invalid(String)

    var message: String {
        switch self {
        case .empty: return "Enter the ground station address, for example ws://192.168.1.20:5555."
        case let .invalid(text): return text
        }
    }
}

// MARK: - View state

/// The connection as the Ground tab shows it.
enum GroundLinkStatus: Equatable {
    /// No address or no key saved yet.
    case notConfigured
    /// Configured, but the user switched the link off.
    case off
    /// The app is in the background; the link is closed until it returns.
    case paused
    case connecting(attempt: Int)
    case connected
    case reconnecting(attempt: Int, retryInSeconds: Double, reason: String)
    /// The ground refused the key. Distinct from network loss; the client waits for the user.
    case wrongKey
    case stopped

    var isConnected: Bool { self == .connected }

    var text: String {
        switch self {
        case .notConfigured: return "Not configured: add the ground station address and key in Settings."
        case .off: return "Ground link off. Switch it on in Settings."
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
        case .connecting, .reconnecting: return "RECONNECTING"
        case .wrongKey: return "WRONG KEY"
        case .notConfigured: return "NOT SET UP"
        case .off: return "OFF"
        case .paused, .stopped: return "OFFLINE"
        }
    }
}

/// The visible result of one command.
struct GroundCommandFeedback: Equatable {
    enum Phase: Equatable {
        /// The tap was taken; the command is on its way to the connection.
        case sending
        /// Handed to the connection. Not proof of delivery: the ground's answer is.
        case handedOver
        /// The ground accepted it and its final answer is still due.
        case accepted
        /// No answer within the reply timeout. The operator may tap again.
        case noReplyYet
        case succeeded
        case failed
    }

    var phase: Phase
    var text: String
    var detail: String?
    let startedAt: Date
    var updatedAt: Date

    var isFailure: Bool { phase == .failed }
    var isWaiting: Bool { phase == .sending || phase == .handedOver }
    /// Whether to offer a "send again" button next to the text. A second tap is simply
    /// another command; nothing is retried automatically.
    var offersRetry: Bool { phase == .noReplyYet || phase == .failed }
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
    /// "MANUAL", "HELD" or nil.
    let controlChip: String?
    /// The control text with the HELD countdown counted down locally; empty for none.
    let controlText: String
    let isPartial: Bool
    /// True when the values are not live: the whole state is stale or the drone is DOWN or LOST.
    let isStale: Bool
    /// Why the values are not live, or nil.
    let staleNote: String?
    let canResume: Bool
    let landFeedback: GroundCommandFeedback?
    let resumeFeedback: GroundCommandFeedback?

    var id: String { card.id }
}

/// Text the Ground tab adds to `GroundText`.
enum GroundUIText {
    static let landAllHandedOver = GroundText.landAllSending + " Handed to the connection; waiting for the ground's reply."
    static let landAllNotSentLinkOff =
        "LAND ALL was not sent: the ground station link is off or not set up. Use the laptop's LAND ALL (Shift+L) or the drones' own controls."
    static let landAllSendFailed =
        "LAND ALL was not sent: the connection failed while sending. Use the laptop's LAND ALL (Shift+L) or the drones' own controls."
    static let staleAfterDisconnect = "Last values: not connected to the ground."
    static let staleRefreshing = "Last values: waiting for a fresh fleet status from the ground."
    static let partialRecord = "Partial record: only manual-control data so far; waiting for the fleet status."
    static let privacyNote = "Connects only to the ground station address you enter."

    static func droneNotSent(_ id: String, action: String, linkOff: Bool) -> String {
        let why = linkOff ? "the ground station link is off or not set up" : "not connected to the ground"
        return "\(id): \(action) was not sent: \(why). Use the laptop or the drone's own controls."
    }

    static func droneSendFailed(_ id: String, action: String) -> String {
        "\(id): \(action) was not sent: the connection failed while sending. Use the laptop or the drone's own controls."
    }

    static func droneSending(_ id: String, action: String) -> String { "\(id): \(action) sending\u{2026}" }

    static func droneHandedOver(_ id: String, action: String) -> String {
        "\(id): \(action) sending\u{2026} Handed to the connection; waiting for the ground's reply."
    }

    static func droneNoReplyYet(_ id: String, action: String) -> String {
        "\(id): \(action): no reply from the ground yet. Check the connection."
    }

    static func heldCountdown(_ seconds: Double) -> String {
        "HELD: auto-landing in \(GroundText.jsNumber(seconds)) s, no controller"
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
    /// How long a successful LAND ALL text stays. Failures stay until dismissed.
    static let successFadeAfter: TimeInterval = 10
    static let warningFadeAfter: TimeInterval = 8
    static let maxBanners = 10
    static let maxToasts = 5
    static let maxDetections = 50

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
    private(set) var groundStatus: StatusPayload?
    private(set) var landAllFeedback: GroundCommandFeedback?
    /// Keyed by drone id.
    private(set) var landFeedback: [String: GroundCommandFeedback] = [:]
    private(set) var resumeFeedback: [String: GroundCommandFeedback] = [:]
    private(set) var banners: [GroundBanner] = []
    private(set) var toasts: [GroundToast] = []
    /// The time the countdowns and fades are computed against; moved by `tick(now:)`.
    private(set) var now: Date

    // State for the views of later units (video tiles, plan grid, profiles, ceiling,
    // reasoning, match alerts). Filled from the event switch below.
    /// The latest `frame` per drone id (`""` for a frame without one).
    private(set) var latestFrames: [String: FramePayload] = [:]
    private(set) var latestFrameAt: [String: Date] = [:]
    private(set) var fleetPlan: FleetPlanPayload?
    private(set) var fleetDetections: [FleetDetection] = []
    private(set) var detectionProfiles: DetectionProfilesPayload?
    private(set) var ceilingSettings: CeilingSettingsPayload?
    private(set) var reasoningProvider: ReasoningProviderPayload?
    private(set) var matchAlerts: [MatchAlert] = []
    private(set) var streamVideoEnabled: Bool?

    // Settings, mirrored for the views.
    private(set) var savedAddress: String?
    private(set) var hasSavedKey: Bool
    private(set) var isEnabled: Bool

    var isConfigured: Bool { savedAddress != nil && hasSavedKey }

    // MARK: Dependencies

    @ObservationIgnored private let keyStore: any GroundKeyStore
    @ObservationIgnored private let settings: GroundConnectionSettings
    @ObservationIgnored private let clientFactory: GroundClientFactory
    @ObservationIgnored private let http: any GroundHTTPClient
    @ObservationIgnored private let clock: () -> Date
    @ObservationIgnored private let logSink: GroundLogSink

    @ObservationIgnored private var client: GroundClient?
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var appInBackground = false
    @ObservationIgnored private let lifetime = Lifetime()
    /// The value and arrival time of each drone's latest `orphan_land_in_s`.
    @ObservationIgnored private var orphanBase: [String: (value: Double, at: Date)] = [:]
    /// When the state became stale; countdowns freeze there.
    @ObservationIgnored private var staleSince: Date?
    /// Drones whose land was sent without a fleet; a `command_ack` answers the newest.
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
        hasSavedKey = keyStore.hasKey
        isEnabled = settings.enabled
        linkStatus = Self.idleStatus(configured: savedAddress != nil && hasSavedKey, enabled: isEnabled, background: false)
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

    /// Reconnects when the link is configured and switched on. State stays stale until a
    /// fresh `fleet_status` arrives on the new connection.
    func appWillEnterForeground() {
        appInBackground = false
        if client == nil {
            linkStatus = Self.idleStatus(configured: isConfigured, enabled: isEnabled, background: false)
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
        hasSavedKey = keyStore.hasKey
        restartIfWanted()
        return nil
    }

    /// Deletes the saved key and closes the link.
    func removeKey() {
        keyStore.deleteKey()
        hasSavedKey = keyStore.hasKey
        closeClient()
        markStale(GroundUIText.staleAfterDisconnect)
        linkStatus = Self.idleStatus(configured: isConfigured, enabled: isEnabled, background: appInBackground)
    }

    /// Switches the link on or off. On connects only when an address and a key are saved.
    func setEnabled(_ enabled: Bool) {
        settings.enabled = enabled
        isEnabled = enabled
        if enabled {
            connectIfWanted()
            if client == nil {
                linkStatus = Self.idleStatus(configured: isConfigured, enabled: true, background: appInBackground)
            }
        } else {
            if client != nil {
                closeClient()
                markStale(GroundUIText.staleAfterDisconnect)
            }
            linkStatus = Self.idleStatus(configured: isConfigured, enabled: false, background: appInBackground)
        }
    }

    /// "Test connection": the REST health probe with the saved address and key. The text never
    /// contains the key.
    func testConnection() async -> String {
        guard let address = savedAddress, case let .success(endpoint) = GroundAddress.validate(address) else {
            return "Save the ground station address first."
        }
        guard let key = keyStore.loadKey() else { return "Save the dashboard API key first." }
        let result = await GroundHealthProbe(endpoint: endpoint, apiKey: key, http: http).run()
        switch result {
        case let .ok(version):
            return "Reachable, key accepted" + (version.map { " (ground version \($0))" } ?? "") + "."
        case let .wrongKey(version):
            return "Reachable, but the ground refused the key" + (version.map { " (ground version \($0))" } ?? "") + "."
        case let .unreachable(detail):
            return "Unreachable: \(detail). Check the address and that the phone is on the ground's network."
        case let .unexpectedStatus(status):
            return "The address answered with HTTP \(status); it may not be a FlightRisk ground station."
        case let .invalidResponse(detail):
            return "Not a FlightRisk ground station response: \(detail)."
        }
    }

    /// The banner's "Retry": ends a wrong-key or backoff wait at once.
    func retryConnection() {
        if let client {
            Task { await client.retryNow() }
        } else {
            connectIfWanted()
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
        let started = clock()
        landAllFeedback = GroundCommandFeedback(phase: .sending, text: GroundText.landAllSending, startedAt: started, updatedAt: started)
        let outcome = await emit(.landAll)
        let at = clock()
        guard landAllFeedback?.startedAt == started else { return }  // A newer tap or answer took over.
        switch outcome {
        case .sent:
            if landAllFeedback?.phase == .sending {
                landAllFeedback?.phase = .handedOver
                landAllFeedback?.text = GroundUIText.landAllHandedOver
                landAllFeedback?.updatedAt = at
            }
        case .notConnected:
            landAllFeedback = failure(GroundText.landAllNotSent, startedAt: started)
        case .noClient, .shutDown:
            landAllFeedback = failure(GroundUIText.landAllNotSentLinkOff, startedAt: started)
        case .sendFailed, .refused:
            landAllFeedback = failure(GroundUIText.landAllSendFailed, startedAt: started)
        }
    }

    /// Lands one drone (`drone_command {command: "land"}`). Always enabled while the card exists,
    /// whatever the drone's link or control state. An empty id is refused without sending.
    func landDrone(_ id: String) async {
        await droneCommand(.landDrone(droneId: id), id: id, action: "Land")
    }

    /// Resumes the search of a held drone nobody controls (`manual_resume_search`). Sent only
    /// when `canResume(id)` is true.
    func resumeHeldDrone(_ id: String) async {
        guard canResume(id) else {
            log(.warning, "resume not offered for this drone; nothing sent")
            return
        }
        await droneCommand(.resumeHeldDrone(droneId: id), id: id, action: "Resume search")
    }

    /// Whether to show "Resume search": the reducer says the drone is held and nobody controls
    /// it, the state is live, and the link is up.
    func canResume(_ id: String) -> Bool {
        reducer.canResume(id) && !isStateStale && linkStatus.isConnected
    }

    func dismissLandAllFeedback() { landAllFeedback = nil }
    func dismissBanner(_ id: UUID) { banners.removeAll { $0.id == id } }
    func dismissToast(_ id: UUID) { toasts.removeAll { $0.id == id } }

    // MARK: Derived view state

    var droneCards: [GroundDroneCardModel] {
        reducer.cards.map { card in
            let id = card.id
            let partial = reducer.isPartial(id)
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
            if isStateStale { note = staleNote ?? GroundUIText.staleAfterDisconnect }
            else if let s = card.staleNote { note = s }
            return GroundDroneCardModel(
                card: card,
                controlChip: chip,
                controlText: controlText,
                isPartial: partial,
                isStale: isStateStale || card.link.isStale,
                staleNote: note,
                canResume: canResume(id),
                landFeedback: landFeedback[id],
                resumeFeedback: resumeFeedback[id]
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

    /// The HELD countdown, counted down locally from the latest server value as the contract
    /// says, and frozen while the state is stale.
    private func countdownSeconds(for id: String, card: DroneCardState) -> Double {
        let received = card.control.orphanLandInS ?? 0
        guard let base = orphanBase[id], base.value == received else { return max(0, received) }
        let reference = staleSince ?? now
        return OrphanCountdown.remaining(received: base.value, elapsedSeconds: reference.timeIntervalSince(base.at))
    }

    // MARK: Ticking

    /// Moves `now` (countdowns), shows "no reply yet" for commands without an answer after
    /// `replyTimeout`, and fades successful LAND ALL texts and warnings. Failures never fade.
    func tick(now date: Date) {
        now = date
        if var f = landAllFeedback {
            if f.isWaiting, date.timeIntervalSince(f.startedAt) >= Self.replyTimeout {
                f.phase = .noReplyYet
                f.text = GroundText.landAllNoReplyYet
                f.updatedAt = date
                landAllFeedback = f
            } else if f.phase == .succeeded, date.timeIntervalSince(f.updatedAt) >= Self.successFadeAfter {
                landAllFeedback = nil
            }
        }
        for (id, f) in landFeedback where f.isWaiting && date.timeIntervalSince(f.startedAt) >= Self.replyTimeout {
            landFeedback[id] = noReply(f, id: id, action: "Land", at: date)
        }
        for (id, f) in resumeFeedback where f.isWaiting && date.timeIntervalSince(f.startedAt) >= Self.replyTimeout {
            resumeFeedback[id] = noReply(f, id: id, action: "Resume search", at: date)
        }
        toasts.removeAll { !$0.isError && date.timeIntervalSince($0.receivedAt) >= Self.warningFadeAfter }
    }

    // MARK: Event handling

    /// Applies one client event. Every server event goes through here in arrival order.
    func handle(_ event: SocketIOMini.GroundEvent) {
        switch event {
        case let .stateChanged(state):
            apply(state)
        case .reconnected:
            markStale(GroundUIText.staleRefreshing)
            Task { await self.sendStateRequests() }
        case let .eventsDropped(count):
            log(.warning, "\(count) events were dropped; requesting the full state again")
            markStale(GroundUIText.staleRefreshing)
            Task {
                if let client = self.client { self.apply(await client.state) }
                await self.sendStateRequests()
            }
        case let .server(serverEvent):
            guard let payload = serverEvent.payload else { return }
            handle(GroundProtocol.GroundEvent.decode(name: serverEvent.name, payload: payload))
        }
    }

    /// Applies one decoded server event.
    func handle(_ event: GroundProtocol.GroundEvent) {
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
                if linkStatus.isConnected { clearStale() }
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
                    landAllFeedback = GroundCommandFeedback(phase: .failed, text: info.text, startedAt: landAllFeedback?.startedAt ?? at, updatedAt: at)
                }
            }
            if let refusal = GroundText.fleetRefusal(ev) {
                addBanner(refusal, isFailure: true, at: at)
            }
        case let .fleetPlan(p):
            fleetPlan = p
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
            if let id = pendingLegacyLands.popLast() {
                landFeedback[id] = GroundCommandFeedback(
                    phase: .succeeded, text: "\(id): Land accepted by the ground.",
                    startedAt: landFeedback[id]?.startedAt ?? at, updatedAt: at
                )
            }
        case let .streamVideoState(s):
            streamVideoEnabled = s.enabled
        case let .error(e):
            addToast(e.message ?? "The ground reported an error.", isError: true, at: at)
        case let .warning(e):
            addToast(e.message ?? "The ground reported a warning.", isError: false, at: at)
        case .unknown, .undecodable:
            break
        }
    }

    // MARK: Private: connection

    private static func idleStatus(configured: Bool, enabled: Bool, background: Bool) -> GroundLinkStatus {
        if !configured { return .notConfigured }
        if !enabled { return .off }
        if background { return .paused }
        return .stopped
    }

    private func connectIfWanted() {
        guard client == nil, isEnabled, !appInBackground else { return }
        guard let address = savedAddress, case let .success(endpoint) = GroundAddress.validate(address) else {
            linkStatus = .notConfigured
            return
        }
        // The key goes from the Keychain straight into the client and is not kept here.
        guard let key = keyStore.loadKey() else {
            linkStatus = .notConfigured
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
        let frameTask = Task { [weak self] in
            for await frame in frames {
                guard let self else { return }
                guard self.generation == current else { return }
                guard let payload = frame.payload else { continue }
                if case let .frame(f) = GroundProtocol.GroundEvent.decode(name: frame.name, payload: payload) {
                    self.storeFrame(f, at: self.clock())
                }
            }
        }
        lifetime.replaceStreamTasks([eventTask, frameTask])
        Task { await newClient.connect() }
    }

    private func closeClient() {
        guard let old = client else { return }
        client = nil
        generation += 1
        lifetime.setClient(nil)
        lifetime.replaceStreamTasks([])
        Task { await old.shutdown() }
    }

    private func restartIfWanted() {
        if client != nil {
            closeClient()
            markStale(GroundUIText.staleAfterDisconnect)
        }
        connectIfWanted()
        if client == nil {
            linkStatus = Self.idleStatus(configured: isConfigured, enabled: isEnabled, background: appInBackground)
        }
    }

    /// A different ground: nothing from the old one may stay on screen.
    private func forgetGroundState() {
        reducer.reset()
        orphanBase = [:]
        groundStatus = nil
        latestFrames = [:]
        latestFrameAt = [:]
        fleetPlan = nil
        fleetDetections = []
        detectionProfiles = nil
        ceilingSettings = nil
        reasoningProvider = nil
        matchAlerts = []
        streamVideoEnabled = nil
        landFeedback = [:]
        resumeFeedback = [:]
        pendingLegacyLands = []
    }

    private func apply(_ state: GroundConnectionState) {
        switch state {
        case .idle:
            break
        case let .connecting(attempt):
            linkStatus = .connecting(attempt: attempt)
            markStale(GroundUIText.staleAfterDisconnect)
        case .connected:
            linkStatus = .connected
        case let .reconnecting(attempt, retryIn, reason):
            let seconds = Double(retryIn.components.seconds) + Double(retryIn.components.attoseconds) / 1e18
            linkStatus = .reconnecting(attempt: attempt, retryInSeconds: seconds, reason: reason.description)
            markStale(GroundUIText.staleAfterDisconnect)
        case .failed:
            linkStatus = .wrongKey
            markStale(GroundUIText.staleAfterDisconnect)
        case .stopped:
            linkStatus = Self.idleStatus(configured: isConfigured, enabled: isEnabled, background: appInBackground)
            markStale(GroundUIText.staleAfterDisconnect)
        }
    }

    private func markStale(_ note: String) {
        if !isStateStale { staleSince = clock() }
        isStateStale = true
        staleNote = note
    }

    private func clearStale() {
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

    private func droneCommand(_ command: GroundCommand, id: String, action: String) async {
        guard command.isSendable else {
            log(.warning, "refused \(command.eventName) with an empty drone id")
            return
        }
        let isLand: Bool
        if case .landDrone = command { isLand = true } else { isLand = false }
        let started = clock()
        setDroneFeedback(isLand, id, GroundCommandFeedback(phase: .sending, text: GroundUIText.droneSending(id, action: action), startedAt: started, updatedAt: started))
        let outcome = await emit(command)
        let at = clock()
        guard droneFeedback(isLand, id)?.startedAt == started else { return }
        switch outcome {
        case .sent:
            if droneFeedback(isLand, id)?.phase == .sending {
                setDroneFeedback(isLand, id, GroundCommandFeedback(phase: .handedOver, text: GroundUIText.droneHandedOver(id, action: action), startedAt: started, updatedAt: at))
            }
            if isLand, reducer.isLegacyShape { pendingLegacyLands.append(id) }
        case .notConnected:
            setDroneFeedback(isLand, id, failure(GroundUIText.droneNotSent(id, action: action, linkOff: false), startedAt: started))
        case .noClient, .shutDown:
            setDroneFeedback(isLand, id, failure(GroundUIText.droneNotSent(id, action: action, linkOff: true), startedAt: started))
        case .sendFailed, .refused:
            setDroneFeedback(isLand, id, failure(GroundUIText.droneSendFailed(id, action: action), startedAt: started))
        }
    }

    private func droneFeedback(_ isLand: Bool, _ id: String) -> GroundCommandFeedback? {
        isLand ? landFeedback[id] : resumeFeedback[id]
    }

    private func setDroneFeedback(_ isLand: Bool, _ id: String, _ value: GroundCommandFeedback) {
        if isLand { landFeedback[id] = value } else { resumeFeedback[id] = value }
    }

    private func failure(_ text: String, startedAt: Date) -> GroundCommandFeedback {
        GroundCommandFeedback(phase: .failed, text: text, startedAt: startedAt, updatedAt: clock())
    }

    private func noReply(_ f: GroundCommandFeedback, id: String, action: String, at: Date) -> GroundCommandFeedback {
        var copy = f
        copy.phase = .noReplyYet
        copy.text = GroundUIText.droneNoReplyYet(id, action: action)
        copy.updatedAt = at
        return copy
    }

    // MARK: Private: answers

    private func applyLandAllAck(_ ack: LandAllAck, at: Date) {
        let result = GroundText.landAllResult(ack)
        let started = landAllFeedback?.startedAt ?? at
        if landAllFeedback?.phase == .failed, !result.isFailure {
            // A failure reported for this LAND ALL stays visible; the counts are added to it.
            landAllFeedback?.detail = result.text
            landAllFeedback?.updatedAt = at
            return
        }
        landAllFeedback = GroundCommandFeedback(
            phase: result.isFailure ? .failed : .succeeded, text: result.text, startedAt: started, updatedAt: at
        )
    }

    private func applyManualReply(_ reply: ManualReply, final: Bool, at: Date) {
        guard let id = reply.droneId, !id.isEmpty else { return }
        let isLand: Bool
        let action: String
        switch reply.action {
        case .land?: isLand = true; action = "Land"
        case .resumeSearch?: isLand = false; action = "Resume search"
        default: return
        }
        let started = droneFeedback(isLand, id)?.startedAt ?? at
        if let refusal = GroundText.manualRefusal(reply) {
            let text = isLand ? refusal + ". Land the drone with its own controls." : refusal + "."
            setDroneFeedback(isLand, id, GroundCommandFeedback(phase: .failed, text: text, startedAt: started, updatedAt: at))
            return
        }
        guard reply.ok == true else { return }
        if final {
            let text = isLand ? "\(id): Land acknowledged by the drone's edge." : "\(id): search resumed."
            setDroneFeedback(isLand, id, GroundCommandFeedback(phase: .succeeded, text: text, startedAt: started, updatedAt: at))
        } else if droneFeedback(isLand, id)?.phase != .succeeded {
            let text = "\(id): \(action) accepted by the ground; waiting for the drone's edge."
            setDroneFeedback(isLand, id, GroundCommandFeedback(phase: .accepted, text: text, startedAt: started, updatedAt: at))
        }
    }

    private func storeFrame(_ frame: FramePayload, at: Date) {
        let id = frame.droneId ?? ""
        latestFrames[id] = frame
        latestFrameAt[id] = at
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
        if toasts.count > Self.maxToasts { toasts.removeLast(toasts.count - Self.maxToasts) }
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
