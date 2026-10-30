import XCTest
@testable import GroundProtocol

/// Tests for the review round: crash safety, LAND ALL outcomes, event ordering, strict types,
/// per-element decoding, unsendable commands and the nits.
final class ReviewRoundTests: XCTestCase {
    private func decode<T: Decodable>(_ t: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(t, from: Data(json.utf8))
    }

    // MARK: B1 large and odd numbers never trap

    func testEveryIntFieldReadsHugeAndFractionalNumbersAsNil() throws {
        for big in ["1e20", "-1e19", "1e300", "9223372036854775808", "4294967296", "2.5", "\"7\"", "true", "\"NaN\""] {
            XCTAssertNil(try decode(FleetStatusPayload.self, #"{"count":\#(big)}"#).count, big)
            XCTAssertNil(try decode(StatusPayload.self, #"{"fleet":{"drones":\#(big)}}"#).fleet?.drones, big)
            let rec = try decode(DroneRecord.self, #"{"lane_index":\#(big),"lanes_total":\#(big)}"#)
            XCTAssertNil(rec.laneIndex, big); XCTAssertNil(rec.lanesTotal, big)
            let det = try decode(FleetDetection.self, #"{"id":\#(big),"count":\#(big)}"#)
            XCTAssertNil(det.id, big); XCTAssertNil(det.count, big)
            XCTAssertNil(try decode(FramePayload.self, #"{"persons":\#(big)}"#).persons, big)
            XCTAssertNil(try decode(PlanLane.self, #"{"index":\#(big)}"#).index, big)
            XCTAssertNil(try decode(UncoveredEntry.self, #"{"index":\#(big)}"#).index, big)
            XCTAssertNil(try decode(MatchAlert.self, #"{"renotify_count":\#(big)}"#).renotifyCount, big)
            let ack = try decode(LandAllAck.self, #"{"coordinator":\#(big),"transport":\#(big),"local":\#(big),"reached_any":true}"#)
            XCTAssertNil(ack.coordinator, big); XCTAssertNil(ack.transport, big); XCTAssertNil(ack.local, big)
            XCTAssertEqual(ack.reachedAny, true)
            XCTAssertEqual(try decode(FleetEvent.self, #"{"to":{"d":\#(big)}}"#).to, [:], big)
        }
        // The critic's three crashing inputs, through the event decoder.
        if case .fleetStatus(let s) = GroundEvent.decode(name: "fleet_status", payload: Data(#"{"count":1e20,"drones":{}}"#.utf8)) {
            XCTAssertNil(s.count)
            XCTAssertEqual(s.drones, [:])
        } else { XCTFail() }
        if case .fleetStatus(let s) = GroundEvent.decode(name: "fleet_status", payload: Data(#"{"drones":{"d1":{"lane_index":1e300}}}"#.utf8)) {
            XCTAssertNil(s.drones?["d1"]?.laneIndex)
        } else { XCTFail() }
    }

    func testWholeNumbersInRangeStillDecode() throws {
        XCTAssertEqual(try decode(FleetStatusPayload.self, #"{"count":2}"#).count, 2)
        XCTAssertEqual(try decode(FleetStatusPayload.self, #"{"count":2.0}"#).count, 2)
        XCTAssertEqual(try decode(FleetStatusPayload.self, #"{"count":-3}"#).count, -3)
    }

    // MARK: B2 and B3 LAND ALL outcomes

    func testLandAllIsASuccessOnlyWhenReachedAnyIsTrue() throws {
        let failText = "LAND ALL FAILED: no drone was reached. Land each drone by hand (per-drone Land, or the drone's own controls)."
        for json in ["{}", #"{"reached_any":null,"coordinator":0}"#, #"{"reached_any":"false"}"#, #"{"reached_any":"true"}"#,
                     #"{"reached_any":0}"#, #"{"reached_any":1}"#, #"{"reached_any":false}"#] {
            let r = GroundText.landAllResult(try decode(LandAllAck.self, json))
            XCTAssertTrue(r.isFailure, json)
            XCTAssertEqual(r.text, failText, json)
        }
        let ok = GroundText.landAllResult(try decode(LandAllAck.self, #"{"reached_any":true,"coordinator":2}"#))
        XCTAssertFalse(ok.isFailure)
        XCTAssertEqual(ok.text, "LAND ALL reached drones (coordinator 2)")
    }

    func testFailedLandAllEventIsAFailureNotSent() throws {
        let ev = try decode(FleetEvent.self, #"{"event":"land_all","detail":"failed: drone-2"}"#)
        let b = try XCTUnwrap(GroundText.fleetEventBannerInfo(ev))
        XCTAssertTrue(b.isFailure)
        XCTAssertEqual(b.text, "LAND ALL FAILED: failed: drone-2. Land each drone by hand (per-drone Land, or the drone's own controls).")
        let sent = try XCTUnwrap(GroundText.fleetEventBannerInfo(try decode(FleetEvent.self, #"{"event":"land_all","detail":"sent to all"}"#)))
        XCTAssertFalse(sent.isFailure)
        XCTAssertEqual(sent.text, "LAND ALL sent")
        let noDetail = try XCTUnwrap(GroundText.fleetEventBannerInfo(try decode(FleetEvent.self, #"{"event":"land_all"}"#)))
        XCTAssertEqual(noDetail.text, "LAND ALL sent")
        let other = try XCTUnwrap(GroundText.fleetEventBannerInfo(try decode(FleetEvent.self, #"{"event":"drone_lost","detail":"failed"}"#)))
        XCTAssertFalse(other.isFailure)
    }

    // MARK: B4 reducer, arrival order

    private func fleetStatus(_ droneJSON: String, controlled: String = "null") throws -> GroundEvent {
        .fleetStatus(try decode(FleetStatusPayload.self, #"{"drones":{"d1":\#(droneJSON)},"manual_controlled":\#(controlled)}"#))
    }
    private func manualStatus(_ entryJSON: String, controlled: String = "null") throws -> GroundEvent {
        .manualStatus(try decode(ManualStatusPayload.self, #"{"controlled":\#(controlled),"drones":{"d1":\#(entryJSON)}}"#))
    }
    private let autonomousEntry = #"{"control_mode":"autonomous","edge_control_mode":"autonomous","manual_last":null,"manual_supported":true,"orphan_land_in_s":null}"#

    func testOlderManualStatusAutonomousThenNewerFleetStatusHeldShowsHeld() throws {
        var r = DroneStateReducer()
        r.apply(try manualStatus(autonomousEntry))
        r.apply(try fleetStatus(#"{"connected":true,"control_mode":"held","edge_control_mode":"held","is_flying":true,"orphan_land_in_s":37}"#))
        XCTAssertEqual(r.card(for: "d1")?.control, .held(orphanLandInS: 37))
        XCTAssertTrue(r.partialDroneIds.isEmpty)
    }

    func testNewerManualStatusStillOverridesOlderFleetStatus() throws {
        var r = DroneStateReducer()
        r.apply(try fleetStatus(#"{"connected":true,"control_mode":"held","orphan_land_in_s":37}"#))
        r.apply(try manualStatus(autonomousEntry))
        XCTAssertEqual(r.card(for: "d1")?.control, .autonomous(.unknown))
    }

    func testCountdownFromOlderManualStatusIsReplacedByNewerFleetStatus() throws {
        let held120 = #"{"control_mode":"held","edge_control_mode":"held","manual_last":null,"manual_supported":true,"orphan_land_in_s":120}"#
        var r = DroneStateReducer()
        r.apply(try manualStatus(held120))
        r.apply(try fleetStatus(#"{"connected":true,"control_mode":"held","edge_control_mode":"held","orphan_land_in_s":37}"#))
        XCTAssertEqual(r.card(for: "d1")?.control.text, "HELD: auto-landing in 37 s, no controller")
        var other = DroneStateReducer()
        other.apply(try fleetStatus(#"{"connected":true,"control_mode":"held","orphan_land_in_s":37}"#))
        other.apply(try manualStatus(held120))
        XCTAssertEqual(other.card(for: "d1")?.control.text, "HELD: auto-landing in 120 s, no controller")
    }

    func testRestartCaseManualStatusBeforeFirstTelemetryThenFleetStatus() throws {
        var r = DroneStateReducer()
        r.apply(try manualStatus(autonomousEntry))
        XCTAssertTrue(r.isPartial("d1"))
        XCTAssertEqual(r.drones["d1"]?.droneId, "d1")
        XCTAssertEqual(r.card(for: "d1")?.link, .unknown)
        r.apply(try fleetStatus(#"{"connected":true,"control_mode":"held","edge_control_mode":"held","is_flying":true,"orphan_land_in_s":90}"#))
        XCTAssertFalse(r.isPartial("d1"))
        XCTAssertEqual(r.card(for: "d1")?.control, .held(orphanLandInS: 90))
        XCTAssertEqual(r.card(for: "d1")?.link, .connected)
    }

    func testFleetStatusRemovesDronesItNoLongerListsAndManualOnlyKeysOverlay() throws {
        var r = DroneStateReducer()
        r.apply(try manualStatus(autonomousEntry))
        r.apply(.fleetStatus(try decode(FleetStatusPayload.self, #"{"drones":{"d2":{"connected":true}}}"#)))
        XCTAssertEqual(r.sortedDroneIds, ["d2"])
        // Only the keys a manual_status carries are replaced.
        r.apply(.manualStatus(try decode(ManualStatusPayload.self, #"{"drones":{"d2":{"orphan_land_in_s":55}}}"#)))
        XCTAssertEqual(r.drones["d2"]?.orphanLandInS, 55)
        XCTAssertEqual(r.drones["d2"]?.connected, true)
        XCTAssertNil(r.drones["d2"]?.controlMode)
        r.apply(.fleetStatus(try decode(FleetStatusPayload.self, #"{"drones":{"d2":{"connected":true}}}"#)))
        XCTAssertNil(r.drones["d2"]?.orphanLandInS, "fleet_status drops the earlier overlay")
    }

    func testReducerTracksManualControlledAndResume() throws {
        var r = DroneStateReducer()
        r.apply(try fleetStatus(#"{"connected":true,"control_mode":"held"}"#, controlled: "null"))
        XCTAssertTrue(r.canResume("d1"))
        r.apply(try manualStatus(#"{"control_mode":"held"}"#, controlled: #""d9""#))
        XCTAssertEqual(r.manualControlled, "d9")
        XCTAssertFalse(r.canResume("d1"))
        r.apply(try fleetStatus(#"{"connected":true,"control_mode":"held"}"#, controlled: #""""#))
        XCTAssertNil(r.manualControlled, "an empty id means nobody")
        r.reset()
        XCTAssertTrue(r.cards.isEmpty)
    }

    // MARK: N1 one bad entry does not drop the collection

    func testOneBadEntryIsSkippedNotTheWholeCollection() throws {
        let s = try decode(FleetStatusPayload.self, #"{"drones":{"d1":{"lost":true},"d2":null,"d3":5}}"#)
        XCTAssertEqual(s.sortedDroneIds, ["d1"])
        XCTAssertEqual(DroneLinkState(try XCTUnwrap(s.drones?["d1"])), .lost)
        let p = try decode(FleetPlanPayload.self, #"{"uncovered":[null,{"kind":"area","w_cm":1},7]}"#)
        XCTAssertEqual(p.uncovered?.count, 1)
        XCTAssertEqual(p.uncovered?.first?.isArea, true)
        let ev = try decode(FleetEvent.self, #"{"event":"profile_switch_pending","drone_ids":["d1",2,null,"d3"]}"#)
        XCTAssertEqual(ev.droneIds, ["d1", "d3"])
        let rp = try decode(ReasoningProviderPayload.self, #"{"available":["gemma",5,"off"],"unavailable":{"claude":"x","bad":3}}"#)
        XCTAssertEqual(rp.available, [.gemma, .off])
        XCTAssertEqual(rp.unavailable, ["claude": "x"])
    }

    func testIntArraysSkipBadElementsWithoutSkippingNeighbours() throws {
        let t = try decode([Int].self, "[1,2]")
        XCTAssertEqual(t, [1, 2])
        struct Box: Codable, Equatable, Sendable { @Lenient var xs: [Int]? }
        XCTAssertEqual(try decode(Box.self, #"{"xs":[1,"a",3,1e20,4]}"#).xs, [1, 3, 4])
    }

    // MARK: N2 strict types, fractional countdown

    func testNumbersAsStringsAreNotNumbers() throws {
        let r = try decode(DroneRecord.self, #"{"orphan_land_in_s":"30","battery":"87","control_mode":"held","height_cm":"100"}"#)
        XCTAssertNil(r.orphanLandInS)
        XCTAssertNil(r.battery)
        XCTAssertNil(r.heightCm)
        XCTAssertEqual(ControlState(id: "d", record: r, manualControlled: nil).text, "HELD: hovering, search paused")
        XCTAssertEqual(DroneCardState(id: "d", record: r, manualControlled: nil).batteryText, "--")
        XCTAssertNil(try decode(DroneRecord.self, #"{"connected":1,"lost":"true","is_flying":"true"}"#).connected)
        XCTAssertNil(try decode(DroneRecord.self, #"{"battery":true}"#).battery)
    }

    func testFractionalCountdownIsShownAsSent() throws {
        let r = try decode(DroneRecord.self, #"{"orphan_land_in_s":29.6,"control_mode":"held"}"#)
        let c = ControlState(id: "d", record: r, manualControlled: nil)
        XCTAssertEqual(c.text, "HELD: auto-landing in 29.6 s, no controller")
        XCTAssertEqual(c.tileBadge, "HELD: auto-landing in 29.6 s, no controller")
        let neg = try decode(DroneRecord.self, #"{"orphan_land_in_s":-5}"#)
        XCTAssertEqual(ControlState(id: "d", record: neg, manualControlled: nil).text, "HELD: auto-landing in -5 s, no controller")
        let whole = try decode(DroneRecord.self, #"{"orphan_land_in_s":30.0}"#)
        XCTAssertEqual(ControlState(id: "d", record: whole, manualControlled: nil).text, "HELD: auto-landing in 30 s, no controller")
    }

    // MARK: N3 unsendable commands

    func testBlankDroneIdsCannotBeSent() {
        for id in ["", " ", "\t\n"] {
            for cmd in [GroundCommand.landDrone(droneId: id), .resumeHeldDrone(droneId: id),
                        .setDetectionProfile(droneId: id, profile: "x")] {
                XCTAssertFalse(cmd.isSendable, "\(cmd)")
                XCTAssertNil(cmd.wireMessage)
                XCTAssertNil(cmd.encodedPayload())
            }
        }
        let ok = GroundCommand.landDrone(droneId: "drone-1").wireMessage
        XCTAssertEqual(ok?.eventName, "drone_command")
        XCTAssertEqual(ok.flatMap { $0.payloadJSON }.map { String(decoding: $0, as: UTF8.self) },
                       #"{"command":"land","drone_id":"drone-1"}"#)
        XCTAssertNotNil(GroundCommand.landAll.wireMessage)
        XCTAssertNil(GroundCommand.landAll.wireMessage?.payloadJSON)
        XCTAssertNotNil(GroundCommand.setFleetDetectionProfile(profile: "").wireMessage)
        XCTAssertNotNil(GroundCommand.setDetectionProfile(droneId: "d", profile: "").wireMessage)
    }

    func testPayloadJSONDoesNotEscapeSlashes() {
        let m = GroundCommand.setFleetDetectionProfile(profile: "a/b").wireMessage
        XCTAssertEqual(m?.payloadJSON.map { String(decoding: $0, as: UTF8.self) }, #"{"profile":"a/b"}"#)
    }

    // MARK: nits

    func testWrongTypedEnumsDoNotHideValidFallbacksOrGiveEmptyText() throws {
        let ev = try decode(FleetEvent.self, #"{"event":5,"type":"drone_lost"}"#)
        XCTAssertNil(ev.event)
        XCTAssertEqual(ev.resolvedKind, .droneLost)
        let reply = try decode(ManualReply.self, #"{"drone_id":"d","ok":false,"reason":5}"#)
        XCTAssertNil(reply.reason)
        XCTAssertEqual(GroundText.manualRefusal(reply), "d: refused")
    }

    func testNumberRoundingMatchesJavaScriptMathRound() {
        XCTAssertEqual(GroundText.number(-0.25), "-0.2")
        XCTAssertEqual(GroundText.number(0.25), "0.3")
        XCTAssertEqual(GroundText.number(-0.05), "0")
        XCTAssertEqual(GroundText.number(-0.04), "0")
        XCTAssertEqual(GroundText.number(2.5), "2.5")
        XCTAssertEqual(GroundText.number(-2.55), "-2.5")
        XCTAssertEqual(GroundText.number(87), "87")
        XCTAssertEqual(GroundText.number(.nan), "--")
    }
}
