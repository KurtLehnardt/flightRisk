import Foundation
import Network
import OSLog

/// Chooses the active ``LlmBackend`` from the user's reasoning settings and
/// monitors connectivity via `NWPathMonitor`.
///
/// Cloud reasoning is opt-in and off by default, as on the dashboard
/// (`flightrisk/reasoning/__init__.py`):
///
/// - The active backend is the provider the user selected in Settings
///   (``ReasoningSettings``), or the default when nothing is selected: Local
///   Gemma when the on-device model is available, otherwise none. The default
///   is never cloud.
/// - Claude is usable only while the user's explicit cloud opt-in is on AND a
///   Claude API key is saved in the Keychain. A saved key alone never turns
///   cloud reasoning on. The key is handed to ``CloudClaudeLlmBackend`` only
///   while both hold, and is cleared from it as soon as either stops holding.
/// - When the selected provider cannot be used, the selector falls back to
///   the on-device backend, or to ``NoOpLlmBackend`` when that is not ready,
///   and records a notice in ``status`` so the user is told. A cloud backend
///   is never chosen as a fallback.
/// - The key is never logged or included in ``status``.
///
/// Register the on-device backend before the cloud backend (see
/// ``registerOnDeviceFirst(local:cloud:)``); the fallback walks the
/// registration order and skips cloud backends.
actor LlmSelector {

    private let logger = Logger(subsystem: "com.flightrisk.app", category: "LlmSelector")

    private var backends: [LlmBackend] = []
    private let noOp = NoOpLlmBackend()
    private var cachedActive: LlmBackend
    private var monitoring = false

    private let settings: ReasoningSettings
    private let keyStore: ReasoningKeyStore

    /// The resolved reasoning state after the last ``refresh()``.
    private(set) var status: ReasoningStatus = .initial
    private var statusHandler: (@Sendable (ReasoningStatus) -> Void)?

    /// NWPathMonitor for connectivity changes.
    private let pathMonitor = NWPathMonitor()
    private let monitorQueue = DispatchQueue(label: "com.flightrisk.app.llm.monitor")

    /// Current internet connectivity status tracked by NWPathMonitor.
    private var hasInternet = true

    // MARK: - Init

    /// - Parameters:
    ///   - settings: The persisted reasoning settings. Legacy values are
    ///     migrated on init (a stored cloud choice without the opt-in flag
    ///     is dropped).
    ///   - keyStore: Where the Claude API key is read from.
    init(
        settings: ReasoningSettings = ReasoningSettings(),
        keyStore: ReasoningKeyStore = KeychainReasoningKeyStore()
    ) {
        settings.migrateLegacyValues()
        self.settings = settings
        self.keyStore = keyStore
        cachedActive = noOp
    }

    // MARK: - Public API

    /// Whether a real (non-NoOp) LLM backend is currently active.
    var isLlmAvailable: Bool {
        cachedActive.name != noOp.name
    }

    /// Names of the registered backends, in registration order.
    var registeredBackendNames: [String] {
        backends.map(\.name)
    }

    /// Register a backend. The fallback walks backends in registration
    /// order, so register the on-device backend first.
    func registerBackend(_ backend: LlmBackend) {
        backends.append(backend)
        logger.debug("Registered backend: \(backend.name, privacy: .public)")
        refresh()
    }

    /// Register the on-device backend first and the cloud backend second.
    func registerOnDeviceFirst(local: LlmBackend, cloud: LlmBackend) {
        registerBackend(local)
        registerBackend(cloud)
    }

    /// Set a handler that receives every change of ``status``. It is called
    /// immediately with the current status.
    func setStatusHandler(_ handler: @escaping @Sendable (ReasoningStatus) -> Void) {
        statusHandler = handler
        handler(status)
    }

    /// The backend that should answer reasoning requests right now.
    func getActiveBackend() -> LlmBackend {
        cachedActive
    }

    /// Re-read the settings and the Keychain, re-evaluate availability and
    /// update the active backend and ``status``. Called on registration, on
    /// connectivity changes and after the user changes a reasoning setting.
    func refresh() {
        let localBackend = backends.first { ReasoningProvider(backendName: $0.name) == .localGemma }
        let cloudBackend = backends.first { ReasoningProvider(backendName: $0.name)?.isCloud == true }

        // Cloud gate: read the key only while the user has opted in, and hand
        // it to the Claude backend only then. Otherwise clear it.
        let optIn = settings.cloudOptIn
        let key = optIn ? keyStore.claudeApiKey() : nil
        if let claude = cloudBackend as? CloudClaudeLlmBackend {
            claude.apiKey = key ?? ""
            let online = hasInternet
            claude.connectivityCheck = { online }
        }

        var unavailable: [ReasoningProvider: String] = [:]
        if !optIn {
            unavailable[.cloudClaude] = ReasoningReason.cloudBlocked
        } else if key == nil {
            unavailable[.cloudClaude] = ReasoningReason.claudeKeyMissing
        } else if cloudBackend == nil {
            unavailable[.cloudClaude] = ReasoningReason.claudeNotRegistered
        } else if !hasInternet || cloudBackend?.isAvailable != true {
            unavailable[.cloudClaude] = ReasoningReason.offline
        }

        let localAvailable = localBackend?.isAvailable == true
        if !localAvailable {
            unavailable[.localGemma] = ReasoningReason.gemmaNotReady
        }

        let selected = settings.selectedProvider(localModelAvailable: localAvailable)
        var active: LlmBackend = noOp
        var activeProvider: ReasoningProvider = .off
        var notice: String?

        switch selected {
        case .off:
            break
        case .localGemma:
            if let localBackend, localAvailable {
                active = localBackend
                activeProvider = .localGemma
            } else {
                notice = "Local Gemma is selected, but \(Self.lowercasedFirst(ReasoningReason.gemmaNotReady)) "
                    + "Reasoning is off until it is ready."
            }
        case .cloudClaude:
            if unavailable[.cloudClaude] == nil, let cloudBackend {
                active = cloudBackend
                activeProvider = .cloudClaude
            } else {
                let reason = unavailable[.cloudClaude] ?? ReasoningReason.claudeNotRegistered
                if let localBackend, localAvailable {
                    active = localBackend
                    activeProvider = .localGemma
                    notice = "Cloud Claude is unavailable: \(reason) Using Local Gemma instead."
                } else {
                    notice = "Cloud Claude is unavailable: \(reason) Reasoning is off."
                }
            }
        }

        let previous = cachedActive
        cachedActive = active
        if previous.name != active.name {
            logger.info("Active backend changed: \(previous.name, privacy: .public) -> \(active.name, privacy: .public)")
            if activeProvider.isCloud {
                logger.notice("Cloud reasoning is on: person crops and the reference photo are sent to Anthropic.")
            }
        }

        let newStatus = ReasoningStatus(
            selected: selected,
            active: activeProvider,
            cloudOptIn: optIn,
            unavailable: unavailable,
            notice: notice
        )
        if newStatus != status {
            status = newStatus
            statusHandler?(newStatus)
        }
    }

    /// Async version of ``refresh()`` that first updates each backend's
    /// cached availability from its actor-isolated state, then re-evaluates.
    func refreshAsync() async {
        for backend in backends {
            if let gemma = backend as? LocalGemmaLlmBackend {
                await gemma.refreshAvailability()
            }
        }
        refresh()
    }

    /// Start monitoring network connectivity changes. When internet is
    /// lost or regained, ``refresh()`` is called automatically.
    ///
    /// Safe to call multiple times; only the first call starts the monitor.
    func startMonitoring() {
        guard !monitoring else { return }
        monitoring = true

        pathMonitor.pathUpdateHandler = { [weak self] path in
            guard let self else { return }
            let satisfied = path.status == .satisfied
            Task {
                await self.handleConnectivityChange(hasInternet: satisfied)
            }
        }

        pathMonitor.start(queue: monitorQueue)
        logger.debug("Connectivity monitoring started")
    }

    /// Stop monitoring network connectivity.
    func stopMonitoring() {
        guard monitoring else { return }
        pathMonitor.cancel()
        monitoring = false
        logger.debug("Connectivity monitoring stopped")
    }

    /// Current internet connectivity status.
    func checkConnectivity() -> Bool {
        hasInternet
    }

    /// Remove all registered backends and reset to NoOp.
    /// Primarily for testing.
    func clear() {
        backends.removeAll()
        cachedActive = noOp
        refresh()
    }

    // MARK: - Internals

    /// Apply a connectivity change. Internal so tests can simulate going
    /// offline without a real network change.
    func handleConnectivityChange(hasInternet: Bool) async {
        let previous = self.hasInternet
        self.hasInternet = hasInternet

        if previous != hasInternet {
            logger.debug("Network \(hasInternet ? "available" : "lost", privacy: .public)")
            await refreshAsync()
        }
    }

    private static func lowercasedFirst(_ text: String) -> String {
        guard let first = text.first else { return text }
        return first.lowercased() + text.dropFirst()
    }
}
