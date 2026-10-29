import Foundation
import XCTest
@testable import SocketIOMini

/// Interoperability with a real FlightRisk dashboard, over `URLSessionWebSocketTask`.
///
/// Skipped unless `FLIGHTRISK_LIVE_TEST_URL` names a throwaway dashboard, for example:
///
/// ```
/// FLIGHTRISK_API_KEY=test-key FLIGHTRISK_CEILING_FILE=$(mktemp) \
///   venv/bin/python -m flightrisk.dashboard --source sim --host 127.0.0.1 --port 6100 --no-llm
/// FLIGHTRISK_LIVE_TEST_URL=ws://127.0.0.1:6100 FLIGHTRISK_LIVE_TEST_KEY=test-key swift test --filter LiveInteropTests
/// ```
///
/// Never point it at a live ground station: the test sends `land_all`.
final class LiveInteropTests: XCTestCase {
    private var endpoint: GroundEndpoint!
    private var key: String!

    override func setUpWithError() throws {
        let environment = ProcessInfo.processInfo.environment
        guard let url = environment["FLIGHTRISK_LIVE_TEST_URL"], !url.isEmpty else {
            throw XCTSkip("Set FLIGHTRISK_LIVE_TEST_URL to run against a throwaway dashboard.")
        }
        endpoint = try GroundEndpoint.parse(url)
        key = environment["FLIGHTRISK_LIVE_TEST_KEY"] ?? "test-key"
    }

    private func makeClient(key: String, logs: LogRecorder) -> GroundClient {
        GroundClient(
            configuration: GroundClient.Configuration(endpoint: endpoint, apiKey: GroundAPIKey(key)),
            logSink: logs.sink
        )
    }

    func testConnectStatusFleetStatusAndLandAllAck() async throws {
        let logs = LogRecorder()
        let client = makeClient(key: key, logs: logs)
        let recorder = EventRecorder(client.events)
        await client.connect()
        try await eventually("connected", timeout: 10) { await client.state.isConnected }
        try await eventually("reconnected signal", timeout: 5) { recorder.reconnects.count == 1 }
        try await eventually("status", timeout: 5) { recorder.serverEvents.contains { $0.name == "status" } }

        for name in GroundClient.stateRequestEvents {
            let result = try await client.emit(name)
            XCTAssertEqual(result, .sent, name)
        }
        try await eventually("fleet_status", timeout: 5) { recorder.serverEvents.contains { $0.name == "fleet_status" } }

        let landAll = try await client.emit("land_all")
        XCTAssertEqual(landAll, .sent)
        try await eventually("land_all_ack", timeout: 40) { recorder.serverEvents.contains { $0.name == "land_all_ack" } }

        let health = await client.healthProbe()
        guard case .ok = health else { return XCTFail("health probe: \(health)") }

        let received = Set(recorder.serverEvents.map(\.name))
        print("LIVE received events: \(received.sorted())")
        for event in recorder.serverEvents where ["status", "fleet_status", "land_all_ack"].contains(event.name) {
            print("LIVE \(event.name): \(String(decoding: event.payload ?? Data(), as: UTF8.self).prefix(300))")
        }
        await client.shutdown()
        XCTAssertFalse(logs.lines.contains { $0.contains(key) })
    }

    func testAWrongKeyIsRefusedAsAKeyProblem() async throws {
        let logs = LogRecorder()
        let client = makeClient(key: "wrong-" + key, logs: logs)
        await client.connect()
        try await eventually("failed", timeout: 10) {
            if case .failed(.authenticationRejected) = await client.state { return true }
            return false
        }
        let state = await client.state
        print("LIVE wrong key state: \(state)")
        let health = await client.healthProbe()
        XCTAssertEqual(health, .wrongKey(version: { if case let .wrongKey(v) = health { return v } else { return nil } }()))
        await client.shutdown()
    }
}
