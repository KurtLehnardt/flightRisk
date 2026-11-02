import Foundation
import GroundProtocol
import SocketIOMini
import XCTest
@testable import FlightRisk

/// Tests for the ground connection settings: address validation, the key in the Keychain
/// (an in-memory fake here), the health probe, and that the key never reaches view state,
/// logs or descriptions. The only key is "test-key".
@MainActor
final class GroundSettingsTests: XCTestCase {

    // MARK: - Address validation

    func testAddressNormalisation() throws {
        let cases: [(String, String)] = [
            ("192.168.1.20", "ws://192.168.1.20:5555"),
            ("192.168.1.20:6000", "ws://192.168.1.20:6000"),
            ("ws://ground.local", "ws://ground.local:5555"),
            ("http://192.168.1.20:5555", "ws://192.168.1.20:5555"),
            ("http://192.168.1.20:5555/", "ws://192.168.1.20:5555"),
            ("  ws://10.0.0.5:7000  ", "ws://10.0.0.5:7000"),
            ("https://ground.example", "wss://ground.example:5555"),
            ("wss://ground.example:8443", "wss://ground.example:8443"),
            ("[::1]:5600", "ws://[::1]:5600"),
        ]
        for (input, expected) in cases {
            guard case let .success(endpoint) = GroundAddress.validate(input) else {
                XCTFail("\(input) should be valid")
                continue
            }
            XCTAssertEqual(endpoint.description, expected, input)
        }
    }

    func testAddressRejections() {
        XCTAssertEqual(GroundAddress.validate(""), .failure(.empty))
        XCTAssertEqual(GroundAddress.validate("   "), .failure(.empty))
        for bad in ["ftp://ground:21", "ws://ground:0", "ws://ground:70000", "ws://ground:5555/path", "ws://user:pw@ground",
                    "ws://ground:5555?x=1", "ws://gro und", "ws://"] {
            guard case let .failure(error) = GroundAddress.validate(bad) else {
                XCTFail("\(bad) should be refused")
                continue
            }
            XCTAssertFalse(error.message.isEmpty)
        }
    }

    func testCleartextPublicAddressIsRefusedWithTheReason() async {
        for text in ["ws://8.8.8.8:5555", "8.8.8.8", "http://100.64.0.1"] {
            guard case let .failure(error) = GroundAddress.validate(text) else {
                XCTFail("\(text) should be refused")
                continue
            }
            XCTAssertTrue(error.message.contains("cleartext is only allowed to local network addresses; use wss:// or https:// for this address"), text)
        }
        XCTAssertNoThrow(try GroundAddress.validate("wss://8.8.8.8").get())
        let harness = GroundHarness()
        let vm = harness.viewModel!
        let message = vm.saveAddress("ws://8.8.8.8:5555")
        XCTAssertNotNil(message)
        XCTAssertNil(vm.savedAddress)
        await harness.tearDown()
    }

    func testASavedAddressThatIsNowRefusedShowsTheRefusalEverywhere() async {
        let harness = GroundHarness()
        harness.defaults.set("ws://8.8.8.8:5555", forKey: GroundConnectionSettings.addressKey)
        harness.defaults.set(true, forKey: GroundConnectionSettings.enabledKey)
        _ = harness.makeViewModel().saveKey(GroundHarness.testKey)
        let vm = harness.makeViewModel()
        let reason = "cleartext is only allowed to local network addresses; use wss:// or https:// for this address"
        guard case let .addressRefused(shown) = vm.linkStatus else { return XCTFail("status was \(vm.linkStatus)") }
        XCTAssertEqual(shown, reason)
        XCTAssertTrue(vm.linkStatus.text.contains(reason))
        XCTAssertFalse(vm.linkStatus.text.contains("Not configured"))
        let test = await vm.testConnection()
        XCTAssertTrue(test.contains(reason))
        XCTAssertFalse(test.contains("Save the ground station address first"))
        await vm.landAll()
        let text = vm.landAllFeedback?.text ?? ""
        XCTAssertTrue(text.contains("LAND ALL was NOT sent"), text)
        XCTAssertTrue(text.contains(reason), text)
        XCTAssertFalse(text.contains(GroundHarness.testKey))
        await harness.tearDown()
    }

    func testSaveAddressPersistsNormalisedValueAndRefusesBadInput() async {
        let harness = GroundHarness()
        let vm = harness.viewModel!
        XCTAssertNotNil(vm.saveAddress(""))
        XCTAssertNil(vm.savedAddress)
        XCTAssertNil(vm.saveAddress("192.168.1.20"))
        XCTAssertEqual(vm.savedAddress, "ws://192.168.1.20:5555")
        XCTAssertEqual(harness.defaults.string(forKey: GroundConnectionSettings.addressKey), "ws://192.168.1.20:5555")
        await harness.tearDown()
    }

    // MARK: - Key handling

    func testKeychainStoreNaming() {
        XCTAssertEqual(KeychainGroundKeyStore.provider, "ground")
        XCTAssertEqual(KeychainGroundKeyStore.contractSecretName, "FLIGHTRISK_DASHBOARD_API_KEY")
    }

    func testKeySaveAndRemove() async {
        let harness = GroundHarness()
        let vm = harness.viewModel!
        XCTAssertFalse(vm.hasSavedKey)
        XCTAssertNotNil(vm.saveKey("   "), "an empty key is refused")
        XCTAssertFalse(harness.keyStore.hasKey)
        XCTAssertNil(vm.saveKey(" test-key \n"))
        XCTAssertTrue(vm.hasSavedKey)
        XCTAssertNotNil(harness.keyStore.loadKey())

        XCTAssertNil(vm.saveAddress(GroundHarness.address))
        vm.setEnabled(true)
        await harness.waitUntil("a transport") { harness.recorder.transports.count == 1 }
        let t = harness.recorder.transports[0]
        t.push(GroundTestFrames.open)
        await harness.waitUntil("connect frame") { t.sent.contains { $0.hasPrefix("40") } }
        // The trimmed key from the store is what the client sends, in the auth object only.
        XCTAssertEqual(t.sent.first { $0.hasPrefix("40") }, #"40{"api_key":"test-key"}"#)

        vm.removeKey()
        XCTAssertFalse(vm.hasSavedKey)
        XCTAssertFalse(harness.keyStore.hasKey)
        XCTAssertEqual(vm.linkStatus, .notConfigured)
        await harness.waitUntil("closed") { t.closed }
        await harness.tearDown()
    }

    func testKeyNeverAppearsInStateLogsOrDescriptions() async {
        let harness = GroundHarness(http: FakeGroundHTTP(statusStatus: 401))
        let vm = harness.viewModel!
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !vm.isStateStale }
        await vm.landAll()
        await vm.landDrone("drone-1")
        t.push(GroundTestFrames.event("error", #"{"message":"refused"}"#))
        let probe = await vm.testConnection()
        t.serverDrop()
        await harness.waitUntil("stale") { vm.isStateStale }

        var texts: [String] = [probe, vm.linkStatus.text, vm.readoutText, String(describing: vm.linkStatus)]
        texts += [vm.landAllFeedback?.text, vm.landAllFeedback?.detail].compactMap { $0 }
        texts += vm.landFeedback.values.map(\.text) + vm.toasts.map(\.text) + vm.banners.map(\.text)
        texts += vm.droneCards.flatMap { [$0.controlText, $0.staleNote ?? ""] }
        var dumped = ""
        dump(vm, to: &dumped)
        texts.append(dumped)
        texts += harness.logs.lines
        if let key = harness.keyStore.loadKey() {
            texts += [String(describing: key), String(reflecting: key)]
        }
        for (index, text) in texts.enumerated() {
            XCTAssertFalse(text.contains(GroundHarness.testKey), "the key leaked into text number \(index)")
        }
        XCTAssertFalse(harness.logs.lines.isEmpty, "the log capture saw the client's lines")
        await harness.tearDown()
    }

    // MARK: - Test connection

    func testHealthProbeUsesTheSavedKeyAndReportsWrongKeyDistinctly() async {
        let wrong = GroundHarness(http: FakeGroundHTTP(statusStatus: 401))
        let unconfigured = await wrong.viewModel.testConnection()
        XCTAssertEqual(unconfigured, "Save the ground station address first.")
        wrong.configure()
        let wrongText = await wrong.viewModel.testConnection()
        XCTAssertEqual(wrongText, "Reachable, but the ground refused the key (ground version 9.9.9).")
        XCTAssertTrue(wrong.http.headers.allSatisfy { $0["Authorization"] == "Bearer test-key" })
        XCTAssertEqual(wrong.recorder.transports.count, 0, "the probe never opens the socket")
        await wrong.tearDown()

        let ok = GroundHarness(http: FakeGroundHTTP())
        ok.configure()
        let okText = await ok.viewModel.testConnection()
        XCTAssertEqual(okText, "Reachable, key accepted (ground version 9.9.9).")
        await ok.tearDown()
    }

    func testTestConnectionNeedsAKey() async {
        let harness = GroundHarness()
        XCTAssertNil(harness.viewModel.saveAddress(GroundHarness.address))
        let text = await harness.viewModel.testConnection()
        XCTAssertEqual(text, "Save the dashboard API key first.")
        XCTAssertTrue(harness.http.headers.isEmpty)
        await harness.tearDown()
    }

    func testPrivacyNoteText() {
        XCTAssertEqual(GroundUIText.privacyNote, "Connects only to the ground station address you enter.")
    }
}
