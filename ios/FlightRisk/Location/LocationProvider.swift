import CoreLocation
import os

/// GPS location provider for tagging match events with coordinates.
///
/// Uses `CLLocationManager` for continuous location updates during search.
/// The last known location is cached and attached to every match entry.
///
/// Callers must ensure location permission is requested before calling
/// ``startUpdates()``. The provider gracefully handles unavailable location
/// (returns nil).
///
/// This is a Swift actor for thread-safe access to mutable state.
/// `CLLocationManagerDelegate` methods are `nonisolated` since they are
/// called by the system on the main thread, and use `Task` to funnel
/// updates back into the actor.
actor LocationProvider: NSObject, CLLocationManagerDelegate {

    // MARK: - Properties

    private let logger = Logger(subsystem: "com.flightrisk", category: "location")

    /// Core Location manager. Must be configured on the main thread.
    private let locationManager = CLLocationManager()

    /// Most recent location from continuous updates.
    private var lastLocation: CLLocation?

    /// Whether continuous updates are currently active.
    private(set) var isActive: Bool = false

    /// Distance filter in meters for location updates.
    /// Default 5m (~5s at walking speed).
    private let distanceFilter: CLLocationDistance

    // MARK: - Initialization

    /// Creates a location provider.
    ///
    /// - Parameter distanceFilter: Minimum distance change in meters to
    ///   trigger a location update. Default 5.0.
    init(distanceFilter: CLLocationDistance = 5.0) {
        self.distanceFilter = distanceFilter
        super.init()
    }

    // MARK: - Public API

    /// Start continuous location updates during active search.
    ///
    /// Safe to call multiple times; only the first call starts updates.
    /// Must be called after location permission is granted.
    func startUpdates() {
        guard !isActive else {
            logger.debug("Location updates already active")
            return
        }

        isActive = true

        // CLLocationManager configuration and delegate must happen on main thread
        let manager = locationManager
        let filter = distanceFilter
        Task { @MainActor in
            manager.delegate = self
            manager.desiredAccuracy = kCLLocationAccuracyBest
            manager.distanceFilter = filter
            manager.requestWhenInUseAuthorization()
            manager.startUpdatingLocation()
        }

        logger.info("Location updates started (distanceFilter: \(self.distanceFilter)m)")
    }

    /// Stop continuous location updates.
    ///
    /// The last known location remains cached and available via
    /// ``getCurrentLocation()`` after stopping.
    func stopUpdates() {
        guard isActive else { return }
        isActive = false

        let manager = locationManager
        Task { @MainActor in
            manager.stopUpdatingLocation()
        }

        logger.info("Location updates stopped")
    }

    /// Get the most recent known location.
    ///
    /// Returns the last location from continuous updates if available,
    /// or the location manager's cached location as a fallback.
    /// May return `nil` if location is unavailable.
    func getCurrentLocation() -> CLLocation? {
        lastLocation ?? locationManager.location
    }

    // MARK: - Internal State Update

    /// Update the cached location from the delegate callback.
    private func setLastLocation(_ location: CLLocation) {
        lastLocation = location
        logger.debug(
            "Location updated: \(location.coordinate.latitude), "
            + "\(location.coordinate.longitude) "
            + "(accuracy: \(location.horizontalAccuracy)m)"
        )
    }

    // MARK: - CLLocationManagerDelegate

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        didUpdateLocations locations: [CLLocation]
    ) {
        guard let location = locations.last else { return }
        Task {
            await self.setLastLocation(location)
        }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        didFailWithError error: Error
    ) {
        let logger = Logger(subsystem: "com.flightrisk", category: "location")
        logger.error("Location update failed: \(error.localizedDescription)")
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let logger = Logger(subsystem: "com.flightrisk", category: "location")
        switch manager.authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways:
            logger.info("Location authorization granted")
        case .denied, .restricted:
            logger.warning("Location authorization denied or restricted")
        case .notDetermined:
            logger.info("Location authorization not yet determined")
        @unknown default:
            logger.info("Location authorization unknown status")
        }
    }
}
