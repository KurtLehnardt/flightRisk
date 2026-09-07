import Foundation
import Network
import os

/// Result of a Tello WiFi check.
enum WifiStatus {
    /// Device is connected to the Tello's WiFi AP.
    case onTelloWifi
    /// Device is on WiFi, but not the Tello's network.
    case onOtherWifi(ssid: String?)
    /// Device has no WiFi connection.
    case noWifi
}

/// Checks whether the device is connected to a Tello drone's WiFi AP.
///
/// The Tello creates a soft-AP with a static gateway at 192.168.10.1.
/// On iOS we cannot inspect the DHCP gateway directly, so this checker
/// sends a UDP probe ("command") to the Tello's well-known address and
/// infers connectivity from whether a response arrives within a short
/// timeout.
actor TelloWifiChecker {

    private let logger = Logger(subsystem: "com.flightrisk", category: "wifi")

    /// Tello command-mode IP address.
    private let telloHost: String
    /// Tello command-mode UDP port.
    private let telloPort: UInt16

    init(telloHost: String = "192.168.10.1", telloPort: UInt16 = 8889) {
        self.telloHost = telloHost
        self.telloPort = telloPort
    }

    // MARK: - Public API

    /// Check the current WiFi connection and return the ``WifiStatus``.
    ///
    /// 1. Uses ``NWPathMonitor`` to verify WiFi is the active interface.
    /// 2. Sends a UDP probe ("command") to the Tello's address with a 1-second
    ///    timeout.
    /// 3. Returns `.onTelloWifi` when a response is received,
    ///    `.onOtherWifi` when WiFi is up but the probe fails, or
    ///    `.noWifi` when there is no WiFi interface.
    func check() async -> WifiStatus {
        // Step 1 -- check WiFi reachability via NWPathMonitor
        let hasWifi = await checkWifiAvailable()
        guard hasWifi else {
            logger.info("No WiFi interface detected")
            return .noWifi
        }

        // Step 2 -- UDP probe to Tello
        let telloReachable = await sendUDPProbe()
        if telloReachable {
            logger.info("Connected to Tello WiFi (probe succeeded)")
            return .onTelloWifi
        }

        logger.info("On other WiFi (probe failed/timed out)")
        return .onOtherWifi(ssid: nil)
    }

    /// Return a user-facing guidance message for the given ``WifiStatus``.
    func getGuidanceMessage(_ status: WifiStatus) -> String {
        switch status {
        case .onTelloWifi:
            return "Connected to Tello WiFi. Ready to fly."
        case .onOtherWifi(let ssid):
            let networkName = ssid ?? "unknown"
            return "Connected to \"\(networkName)\". " +
                "Switch to the Tello WiFi network to connect to the drone."
        case .noWifi:
            return "No WiFi connection. Turn on WiFi and connect to the Tello network."
        }
    }

    // MARK: - Private helpers

    /// Use a one-shot ``NWPathMonitor`` to determine whether WiFi is the
    /// active transport.
    private func checkWifiAvailable() async -> Bool {
        await withCheckedContinuation { continuation in
            let monitor = NWPathMonitor(requiredInterfaceType: .wifi)
            let queue = DispatchQueue(label: "com.flightrisk.wifi.monitor")
            monitor.pathUpdateHandler = { path in
                monitor.cancel()
                continuation.resume(returning: path.status == .satisfied)
            }
            monitor.start(queue: queue)
        }
    }

    /// Send a UDP "command" packet to the Tello and wait up to 1 second for
    /// any response. Returns `true` when a response is received.
    private func sendUDPProbe() async -> Bool {
        let host = NWEndpoint.Host(telloHost)
        let port = NWEndpoint.Port(rawValue: telloPort)!
        let connection = NWConnection(host: host, port: port, using: .udp)

        return await withCheckedContinuation { continuation in
            var resumed = false
            let lock = NSLock()

            func resumeOnce(with value: Bool) {
                lock.lock()
                defer { lock.unlock() }
                guard !resumed else { return }
                resumed = true
                connection.cancel()
                continuation.resume(returning: value)
            }

            let queue = DispatchQueue(label: "com.flightrisk.wifi.probe")

            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    // Connection is ready -- send the probe
                    let data = "command".data(using: .utf8)!
                    connection.send(content: data, completion: .contentProcessed { error in
                        if let error = error {
                            self.logger.warning("UDP probe send failed: \(error.localizedDescription)")
                            resumeOnce(with: false)
                            return
                        }
                        // Wait for a response
                        connection.receive(minimumIncompleteLength: 1,
                                           maximumLength: 1024) { content, _, _, recvError in
                            if recvError != nil || content == nil {
                                resumeOnce(with: false)
                            } else {
                                resumeOnce(with: true)
                            }
                        }
                    })
                case .failed, .cancelled:
                    resumeOnce(with: false)
                default:
                    break
                }
            }

            connection.start(queue: queue)

            // Timeout after 1 second
            queue.asyncAfter(deadline: .now() + 1.0) {
                resumeOnce(with: false)
            }
        }
    }
}
