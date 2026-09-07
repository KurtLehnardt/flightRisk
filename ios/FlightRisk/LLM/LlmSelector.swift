import Foundation
import Network
import OSLog

/// Manages available ``LlmBackend`` instances with priority-based
/// auto-selection and connectivity monitoring via `NWPathMonitor`.
///
/// Backends are tried in registration order (highest priority first).
/// The first backend whose ``LlmBackend/isAvailable`` returns true is the
/// active backend. A ``NoOpLlmBackend`` is always appended as the
/// last-resort fallback.
///
/// When the device loses internet (e.g. switching to Tello WiFi), the
/// selector automatically re-evaluates and falls back. When internet
/// returns, it re-evaluates and may promote a cloud backend.
actor LlmSelector {

    private let logger = Logger(subsystem: "com.flightrisk.app", category: "LlmSelector")

    private var backends: [LlmBackend] = []
    private let noOp = NoOpLlmBackend()
    private var cachedActive: LlmBackend
    private var monitoring = false

    /// NWPathMonitor for connectivity changes.
    private let pathMonitor = NWPathMonitor()
    private let monitorQueue = DispatchQueue(label: "com.flightrisk.app.llm.monitor")

    /// Current internet connectivity status tracked by NWPathMonitor.
    private var hasInternet = true

    // MARK: - Init

    init() {
        cachedActive = noOp
    }

    // MARK: - Public API

    /// Whether a real (non-NoOp) LLM backend is currently available.
    var isLlmAvailable: Bool {
        cachedActive.name != "none"
    }

    /// Register a backend. Backends are checked in registration order,
    /// so register the highest-priority backend first.
    func registerBackend(_ backend: LlmBackend) {
        backends.append(backend)
        refresh()
        logger.debug("Registered backend: \(backend.name)")
    }

    /// Return the highest-priority available backend, falling back to
    /// ``NoOpLlmBackend`` if none are available.
    func getActiveBackend() -> LlmBackend {
        cachedActive
    }

    /// Re-evaluate backend availability and update the cached active
    /// backend. Called automatically on connectivity changes.
    func refresh() {
        let active = backends.first(where: { $0.isAvailable }) ?? noOp
        let previous = cachedActive
        cachedActive = active
        if previous.name != active.name {
            logger.info("Active backend changed: \(previous.name) -> \(active.name)")
        }
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
    }

    // MARK: - Internals

    private func handleConnectivityChange(hasInternet: Bool) {
        let previous = self.hasInternet
        self.hasInternet = hasInternet

        if previous != hasInternet {
            logger.debug("Network \(hasInternet ? "available" : "lost")")
            refresh()
        }
    }
}
