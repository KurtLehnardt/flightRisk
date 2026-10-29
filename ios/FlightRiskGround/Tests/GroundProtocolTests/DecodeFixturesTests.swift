import XCTest
@testable import GroundProtocol

/// R-8: every received payload decodes from its fixture; missing optional fields decode as nil.
final class DecodeFixturesTests: XCTestCase {
    func testThereAreTwentyThreeFixtureFiles() throws {
        let names = try Fixtures.fileNames()
        XCTAssertEqual(names.count, 23, "fixture files: \(names)")
    }

    /// Every case of every file with a server event this build handles decodes into the matching
    /// typed case, never `.unknown` or `.undecodable`.
    func testEveryServerEventCaseOfEveryFileDecodes() throws {
        var decoded = 0
        for file in try Fixtures.fileNames() {
            for c in try Fixtures.cases(file) {
                guard let event = c.event, c.direction == "server_to_client",
                      GroundEvent.handledEventNames.contains(event) else { continue }
                let data = try XCTUnwrap(c.payloadData, "\(file)/\(c.name)")
                let result = GroundEvent.decode(name: event, payload: data)
                switch result {
                case .unknown, .undecodable:
                    XCTFail("\(file)/\(c.name) did not decode: \(result)")
                default:
                    decoded += 1
                }
            }
        }
        XCTAssertGreaterThanOrEqual(decoded, 60)
    }

    /// Every case of every file is at least well formed, including the files this package does not
    /// decode (edge link, Socket.IO frames, vocabularies, client emits).
    func testEveryCaseOfEveryFileIsWellFormed() throws {
        var total = 0
        for file in try Fixtures.fileNames() {
            let (meta, cases) = try Fixtures.load(file)
            XCTAssertNotNil(meta["description"], file)
            XCTAssertFalse(cases.isEmpty, file)
            for c in cases {
                total += 1
                XCTAssertTrue(c.raw["payload"] != nil || c.raw["frame"] != nil, "\(file)/\(c.name)")
            }
        }
        XCTAssertGreaterThan(total, 100)
    }

    func testUnknownEventNamesAreIgnoredNotErrors() {
        for name in ["fleet_telemetry", "plan_preview", "target_photo", "face_report", "something_new"] {
            XCTAssertEqual(GroundEvent.decode(name: name, payload: Data("{}".utf8)), .unknown(name: name))
        }
        XCTAssertEqual(GroundEvent.decode(name: "status", payload: Data("[1]".utf8)), .undecodable(name: "status"))
    }

    // MARK: per-model values

    func testStatus() throws {
        let fleet = try Fixtures.decode(StatusPayload.self, "status", "fleet_two_drones")
        XCTAssertEqual(fleet.connected, true)
        XCTAssertEqual(fleet.source, "edge")
        XCTAssertEqual(fleet.appearanceOnly, true)
        XCTAssertEqual(fleet.tiled, false)
        XCTAssertEqual(fleet.fleet?.drones, 2)
        XCTAssertEqual(fleet.fleet?.net, "station")
        XCTAssertEqual(fleet.fleet?.synced, false)
        XCTAssertEqual(fleet.isFleetMode, true)
        let sim = try Fixtures.decode(StatusPayload.self, "status", "no_fleet_sim_source")
        XCTAssertEqual(sim.isFleetMode, false)
        XCTAssertEqual(sim.source, "sim")
    }

    func testFleetStatusFleetShape() throws {
        let s = try Fixtures.decode(FleetStatusPayload.self, "fleet_status", "all_connected")
        XCTAssertFalse(s.isLegacyShape)
        XCTAssertEqual(s.sortedDroneIds, ["drone-1", "drone-2"])
        XCTAssertEqual(s.net, "station")
        XCTAssertEqual(s.synced, true)
        XCTAssertNil(s.manualControlled)
        let d = try XCTUnwrap(s.drones?["drone-2"])
        XCTAssertEqual(d.droneId, "drone-2")
        XCTAssertEqual(d.altCm, 130)
        XCTAssertEqual(d.battery, 87)
        XCTAssertEqual(d.heightCm, 100)
        XCTAssertEqual(d.isFlying, false)
        XCTAssertEqual(d.headingDeg, 0)
        XCTAssertNil(d.laneIndex)
        XCTAssertEqual(d.relXCm, 250)
        XCTAssertEqual(d.linkAgeS, 0.1)
        XCTAssertEqual(d.controlMode, .autonomous)
        XCTAssertEqual(d.edgeControlMode, .autonomous)
        XCTAssertEqual(d.manualSupported, true)
        XCTAssertNil(d.orphanLandInS)
        XCTAssertNil(d.skewMs)
        XCTAssertEqual(d.offsetMs, 250)
    }

    func testFleetStatusLegacyShape() throws {
        let one = try Fixtures.decode(FleetStatusPayload.self, "fleet_status", "non_fleet_local_drone")
        XCTAssertTrue(one.isLegacyShape)
        XCTAssertEqual(one.count, 1)
        let d = try XCTUnwrap(one.drones?["drone-1"])
        XCTAssertEqual(d.battery, 100)
        XCTAssertEqual(d.height, 0)
        XCTAssertEqual(d.displayHeightCm, 0)
        XCTAssertEqual(d.temperature, 40)
        XCTAssertEqual(d.flightTime, 0)
        XCTAssertEqual(d.isFlying, false)
        XCTAssertEqual(d.isConnected, true)
        XCTAssertNil(d.connected)
        XCTAssertNil(d.lost)
        let none = try Fixtures.decode(FleetStatusPayload.self, "fleet_status", "non_fleet_no_drone")
        XCTAssertEqual(none.count, 0)
        XCTAssertEqual(none.drones, [:])
    }

    func testFleetEventEveryKind() throws {
        let expected: [String: FleetEventKind] = [
            "plan_refused_unconnected": .planRefused, "plan_refused_manual_control": .planRefused,
            "plan_invalidated": .planInvalidated, "start": .start, "start_refused": .startRefused,
            "start_refused_drone_unreachable": .startRefused, "drone_lost": .droneLost,
            "retask": .retask, "retask_failed": .retaskFailed, "land_all": .landAll,
            "land_all_failed": .landAll, "profile_switch_pending": .profileSwitchPending,
        ]
        let cases = try Fixtures.cases("fleet_event")
        XCTAssertEqual(Set(cases.map(\.name)), Set(expected.keys))
        for c in cases {
            let ev = try JSONDecoder().decode(FleetEvent.self, from: XCTUnwrap(c.payloadData))
            XCTAssertEqual(ev.resolvedKind, expected[c.name], c.name)
            XCTAssertEqual(ev.resolvedKind?.isKnown, true, c.name)
        }
        let lost = try Fixtures.decode(FleetEvent.self, "fleet_event", "drone_lost")
        XCTAssertEqual(lost.droneId, "drone-2")
        XCTAssertNil(lost.detail)
        let retask = try Fixtures.decode(FleetEvent.self, "fleet_event", "retask")
        XCTAssertEqual(retask.to, [:])
        XCTAssertEqual(retask.lanes, [])
        XCTAssertEqual(retask.awaiting?.count, 2)
        XCTAssertEqual(retask.awaiting?.first?.direction, .right)
        XCTAssertEqual(retask.awaiting?.first?.endXCm, 325)
        XCTAssertEqual(retask.awaiting?.first?.droneId, "drone-1")
        let failed = try Fixtures.decode(FleetEvent.self, "fleet_event", "retask_failed")
        XCTAssertEqual(failed.lanes?.count, 2)
        XCTAssertEqual(failed.lanes?.first?.reason, "refused by drone-2 (geofence)")
        let pending = try Fixtures.decode(FleetEvent.self, "fleet_event", "profile_switch_pending")
        XCTAssertEqual(pending.droneIds, ["drone-2"])
    }

    func testUnknownFleetEventKindKeepsItsText() throws {
        let ev = try JSONDecoder().decode(FleetEvent.self, from: Data(#"{"event":"brand_new_kind","detail":"x"}"#.utf8))
        XCTAssertEqual(ev.resolvedKind, .unknown("brand_new_kind"))
        XCTAssertEqual(ev.resolvedKind?.isKnown, false)
        XCTAssertEqual(GroundText.fleetEventBanner(ev), "brand_new_kind")
        let viaType = try JSONDecoder().decode(FleetEvent.self, from: Data(#"{"type":"drone_lost","drone_id":"a"}"#.utf8))
        XCTAssertEqual(viaType.resolvedKind, .droneLost)
    }

    func testFleetPlan() throws {
        let p = try Fixtures.decode(FleetPlanPayload.self, "fleet_plan", "two_drones")
        XCTAssertEqual(p.grid?.wCm, 400)
        XCTAssertEqual(p.gapCm, 100)
        XCTAssertEqual(p.stripCm, 75)
        XCTAssertEqual(p.uncovered, [])
        XCTAssertNil(p.details)
        XCTAssertEqual(p.zones?.count, 2)
        let z = try XCTUnwrap(p.zones?[1])
        XCTAssertEqual(z.droneId, "drone-2")
        XCTAssertEqual(z.xCm, 250)
        XCTAssertEqual(z.altCm, 130)
        XCTAssertEqual(z.lost, false)
        XCTAssertEqual(z.lanes?.map { $0.direction }, [.forward, .right, .back])
        XCTAssertEqual(z.lanes?.first?.distanceCm, 400)
    }

    func testFleetPlanMapPlanWithUncoveredKindsAndDetails() throws {
        let p = try Fixtures.decode(FleetPlanPayload.self, "fleet_plan", "map_plan_lost_zone_uncovered")
        let u = try XCTUnwrap(p.uncovered)
        XCTAssertEqual(u.count, 5)
        XCTAssertFalse(u[0].isArea)
        XCTAssertNil(u[0].kind)
        XCTAssertEqual(u[0].index, 1)
        XCTAssertEqual(u[0].direction, .right)
        XCTAssertEqual(u[0].endYCm, 400)
        XCTAssertEqual(u[0].droneId, "drone-1")
        XCTAssertEqual(u[0].reason, "refused by drone-2 (geofence)")
        XCTAssertTrue(u[2].isArea)
        XCTAssertEqual(u[2].kind, "area")
        XCTAssertEqual(u[2].wCm, 290)
        XCTAssertEqual(u[2].hCm, 160)
        let d = try XCTUnwrap(p.details)
        XCTAssertEqual(d.ok, true)
        XCTAssertEqual(d.mode, "fleet")
        XCTAssertEqual(d.pattern, "parallel_track")
        XCTAssertEqual(d.problems, [])
        XCTAssertEqual(d.coverage?.fraction, 0.395)
        XCTAssertEqual(d.geofenceCm, 500)
        XCTAssertEqual(d.takeoffLayout?.count, 2)
        XCTAssertEqual(p.zones?.contains { $0.lost == true }, true)
    }

    func testFleetDetection() throws {
        let d = try Fixtures.decode(FleetDetection.self, "fleet_detection", "new_detection")
        XCTAssertEqual(d.id, 1)
        XCTAssertEqual(d.droneId, "drone-1")
        XCTAssertEqual(d.droneIds, ["drone-1"])
        XCTAssertEqual(d.count, 1)
        XCTAssertEqual(d.xCm, 120)
        XCTAssertEqual(d.relYCm, 80)
        XCTAssertNil(d.lat)
        XCTAssertNotNil(Data(base64Encoded: try XCTUnwrap(d.thumbnail)))
    }

    func testFrameWithVideoAndDetectionsOnly() throws {
        let v = try Fixtures.decode(FramePayload.self, "frame", "edge_video_with_match")
        XCTAssertEqual(v.droneId, "drone-1")
        XCTAssertEqual(v.streamVideo, true)
        XCTAssertEqual(v.match, true)
        XCTAssertEqual(v.matchScore, 0.86)
        XCTAssertEqual(v.persons, 2)
        XCTAssertEqual(v.fps, 0)
        XCTAssertEqual(v.frameWidth, 960)
        XCTAssertEqual(v.frameHeight, 720)
        XCTAssertEqual(v.recording, false)
        XCTAssertEqual(v.boxes?.count, 2)
        XCTAssertEqual(v.boxes?[0].matched, true)
        XCTAssertNil(v.boxes?[0].trackId)
        let corners = try XCTUnwrap(v.boxes?[0].corners)
        XCTAssertEqual(corners.x1, 0.4166666666666667, accuracy: 1e-12)
        XCTAssertEqual(corners.y2, 0.5833333333333334, accuracy: 1e-12)
        let jpeg = try XCTUnwrap(Data(base64Encoded: try XCTUnwrap(v.image)))
        XCTAssertEqual(Array(jpeg.prefix(2)), [0xFF, 0xD8])
        XCTAssertEqual(v.telemetry, FrameTelemetry())
        let o = try Fixtures.decode(FramePayload.self, "frame", "edge_detections_only")
        XCTAssertNil(o.image)
        XCTAssertEqual(o.streamVideo, false)
        XCTAssertEqual(o.boxes?.count, 1)
    }

    func testMatchAlertAndReminder() throws {
        let a = try Fixtures.decode(MatchAlert.self, "match_alert", "edge_match")
        XCTAssertEqual(a.alertId, "00000000000040008000000000000001")
        XCTAssertEqual(a.time, "12:34:56")
        XCTAssertEqual(a.score, 0.86)
        XCTAssertEqual(a.faceScore, 0)
        XCTAssertEqual(a.reidScore, 0.86)
        XCTAssertEqual(a.alertLevel, .possibleMatch)
        XCTAssertEqual(a.trackId, "8_7")
        XCTAssertEqual(a.type, "edge")
        XCTAssertNil(a.reasoning)
        XCTAssertNil(a.renotify)
        XCTAssertNotNil(a.snapshot)
        let r = try Fixtures.decode(MatchAlert.self, "match_alert", "reminder_on_connect")
        XCTAssertEqual(r.renotify, true)
        XCTAssertEqual(r.renotifyCount, 0)
        let unknownLevel = try JSONDecoder().decode(MatchAlert.self, from: Data(#"{"alert_level":"definite"}"#.utf8))
        XCTAssertEqual(unknownLevel.alertLevel, .unknown("definite"))
    }

    func testDetectionProfilesEveryCase() throws {
        for c in try Fixtures.cases("detection_profiles") {
            let p = try JSONDecoder().decode(DetectionProfilesPayload.self, from: XCTUnwrap(c.payloadData))
            for (id, d) in p.drones ?? [:] {
                XCTAssertEqual(d.droneId, id, c.name)
                if let reason = d.refusal?.reason { XCTAssertTrue(reason.isKnown, c.name) }
                if let reason = d.fallbackReason { XCTAssertTrue(reason.isKnown, c.name) }
            }
        }
        let pending = try Fixtures.decode(DetectionProfilesPayload.self, "detection_profiles", "pending")
        XCTAssertEqual(pending.profiles?.count, 3)
        XCTAssertEqual(pending.profiles?[1].name, "top_down")
        XCTAssertEqual(pending.profiles?[1].tiled, true)
        XCTAssertEqual(pending.profiles?[1].minPersonPx, 32)
        XCTAssertEqual(pending.profiles?[0].tiled, nil)
        XCTAssertEqual(pending.controlsEnabled, true)
        XCTAssertEqual(pending.drones?["drone-1"]?.pending, true)
        XCTAssertEqual(pending.drones?["drone-1"]?.override, "top_down")
        let refused = try Fixtures.decode(DetectionProfilesPayload.self, "detection_profiles", "refused")
        XCTAssertEqual(refused.drones?["drone-1"]?.refusal?.reason, .inFlight)
        XCTAssertEqual(refused.drones?["drone-1"]?.refusal?.detail, "detection profiles change only on the ground")
        let fb = try Fixtures.decode(DetectionProfilesPayload.self, "detection_profiles", "fallback")
        XCTAssertEqual(fb.drones?["drone-1"]?.fallbackReason, .weightsMissing)
        let un = try Fixtures.decode(DetectionProfilesPayload.self, "detection_profiles", "unsupported")
        XCTAssertEqual(un.fleetDefault, "default")
        XCTAssertEqual(un.drones?["drone-2"]?.supported, false)
        XCTAssertNil(un.drones?["drone-2"]?.switchable)
        let standby = try Fixtures.decode(DetectionProfilesPayload.self, "detection_profiles", "standby_controls_disabled")
        XCTAssertEqual(standby.controlsEnabled, false)
        XCTAssertNotNil(standby.controlsNote)
        let none = try Fixtures.decode(DetectionProfilesPayload.self, "detection_profiles", "no_board")
        XCTAssertEqual(none.available, false)
        XCTAssertEqual(none.profiles, [])
        XCTAssertEqual(none.drones, [:])
        XCTAssertNil(none.fleetDefault)
        XCTAssertEqual(pending.label(for: "top_down"), "Top-down search (HERIDAL)")
        XCTAssertEqual(pending.label(for: "gone"), "gone")
        XCTAssertEqual(pending.label(for: nil), "--")
    }

    func testCeilingSettingsEveryCase() throws {
        for c in try Fixtures.cases("ceiling_settings") {
            let p = try JSONDecoder().decode(CeilingSettingsPayload.self, from: XCTUnwrap(c.payloadData))
            for d in p.drones ?? [] {
                if let reason = d.edge?.refusal?.reason { XCTAssertTrue(reason.isKnown, c.name) }
            }
        }
        let ok = try Fixtures.decode(CeilingSettingsPayload.self, "ceiling_settings", "on_ground_in_sync")
        XCTAssertEqual(ok.ceilingCm, 200)
        XCTAssertEqual(ok.marginCm, 60)
        XCTAssertEqual(ok.maxSlotCm, 140)
        XCTAssertEqual(ok.limits?.minSlotCm, 20)
        XCTAssertEqual(ok.limits?.maxCeilingCm, 1000)
        XCTAssertEqual(ok.editable, true)
        XCTAssertNil(ok.lockedReason)
        XCTAssertNil(ok.loadError)
        XCTAssertEqual(ok.drones?.count, 2)
        XCTAssertEqual(ok.drones?[1].defaultSlotCm, 130)
        XCTAssertEqual(ok.drones?[1].edge?.inSync, true)
        XCTAssertEqual(ok.drones?[1].edge?.supported, true)
        let refused = try Fixtures.decode(CeilingSettingsPayload.self, "ceiling_settings", "edge_refused")
        XCTAssertEqual(refused.drones?[1].edge?.refusal?.reason, .inFlight)
        XCTAssertEqual(refused.drones?[1].ownSlotCm, 120)
        let above = try Fixtures.decode(CeilingSettingsPayload.self, "ceiling_settings", "slot_above_limit")
        XCTAssertNotNil(above.drones?[1].problem)
        XCTAssertEqual(above.drones?[0].edge?.supported, false)
        XCTAssertNil(above.drones?[0].edge?.ceilingCm)
        let locked = try Fixtures.decode(CeilingSettingsPayload.self, "ceiling_settings", "locked_in_flight")
        XCTAssertEqual(locked.editable, false)
        XCTAssertNotNil(locked.lockedReason)
        let unavailable = try Fixtures.decode(CeilingSettingsPayload.self, "ceiling_settings", "unavailable")
        XCTAssertEqual(unavailable.available, false)
        XCTAssertNil(unavailable.drones)
    }

    func testReasoningProvider() throws {
        let blocked = try Fixtures.decode(ReasoningProviderPayload.self, "reasoning_provider", "local_gemma_cloud_blocked")
        XCTAssertEqual(blocked.provider, .gemma)
        XCTAssertEqual(blocked.label, "Gemma")
        XCTAssertEqual(blocked.model, "gemma4:latest")
        XCTAssertEqual(blocked.available, [.gemma, .off])
        XCTAssertEqual(blocked.cloudAllowed, false)
        XCTAssertEqual(blocked.unavailable?.keys.sorted(), ["claude"])
        let off = try Fixtures.decode(ReasoningProviderPayload.self, "reasoning_provider", "off")
        XCTAssertEqual(off.provider, .off)
        XCTAssertNil(off.label)
        XCTAssertNil(off.model)
        let allowed = try Fixtures.decode(ReasoningProviderPayload.self, "reasoning_provider", "cloud_allowed")
        XCTAssertEqual(allowed.available, [.claude, .gemma, .off])
        XCTAssertEqual(allowed.unavailable, [:])
        let noKey = try Fixtures.decode(ReasoningProviderPayload.self, "reasoning_provider", "cloud_allowed_no_key")
        XCTAssertEqual(noKey.cloudAllowed, true)
        XCTAssertEqual(noKey.unavailable?["claude"], "FLIGHTRISK_CLAUDE_API_KEY is not set.")
        let future = try JSONDecoder().decode(ReasoningProviderPayload.self, from: Data(#"{"provider":"openai","available":["openai","off"]}"#.utf8))
        XCTAssertEqual(future.provider, .unknown("openai"))
        XCTAssertEqual(future.available, [.unknown("openai"), .off])
    }

    func testManualStatusAckAndResult() throws {
        let held = try Fixtures.decode(ManualStatusPayload.self, "manual_status", "held_orphan_countdown")
        XCTAssertNil(held.controlled)
        XCTAssertEqual(held.drones?["drone-2"]?.controlMode, .held)
        XCTAssertEqual(held.drones?["drone-2"]?.orphanLandInS, 120)
        XCTAssertNil(held.drones?["drone-1"]?.orphanLandInS)
        let manual = try Fixtures.decode(ManualStatusPayload.self, "manual_status", "manual_controlled")
        XCTAssertEqual(manual.controlled, "drone-2")
        XCTAssertEqual(manual.drones?["drone-2"]?.controlMode, .manual)

        for (file, name) in [("manual_ack", "land_ok"), ("manual_ack", "land_unknown_drone"),
                             ("manual_ack", "resume_search_ok"), ("manual_ack", "resume_search_not_manual"),
                             ("manual_result", "land_acked"), ("manual_result", "resume_search_acked"),
                             ("manual_result", "land_no_reply")] {
            let r = try Fixtures.decode(ManualReply.self, file, name)
            XCTAssertNotNil(r.droneId, name)
            XCTAssertEqual(r.action?.isKnown, true, name)
            if let reason = r.reason { XCTAssertTrue(reason.isKnown, "\(name): \(reason)") }
        }
        let bad = try Fixtures.decode(ManualAck.self, "manual_ack", "land_unknown_drone")
        XCTAssertEqual(bad.ok, false)
        XCTAssertEqual(bad.reason, .invalid)
        XCTAssertEqual(bad.detail, "'drone-9' is not in the fleet")
        let nr = try Fixtures.decode(ManualResult.self, "manual_result", "land_no_reply")
        XCTAssertEqual(nr.reason, .noReply)
        let resume = try Fixtures.decode(ManualResult.self, "manual_result", "resume_search_acked")
        XCTAssertEqual(resume.action, .resumeSearch)
        XCTAssertEqual(resume.controlMode, .autonomous)
        let notManual = try Fixtures.decode(ManualAck.self, "manual_ack", "resume_search_not_manual")
        XCTAssertEqual(notManual.reason, .notManual)
    }

    func testLandAllAckFlatShape() throws {
        let all = try Fixtures.decode(LandAllAck.self, "land_all_ack", "fleet_reached_all")
        XCTAssertEqual(all.coordinator, 2)
        XCTAssertEqual(all.transport, 2)
        XCTAssertNil(all.local)
        XCTAssertEqual(all.reachedAny, true)
        let local = try Fixtures.decode(LandAllAck.self, "land_all_ack", "local_fleet")
        XCTAssertEqual(local.local, 1)
        XCTAssertNil(local.coordinator)
        let none = try Fixtures.decode(LandAllAck.self, "land_all_ack", "reached_no_drone")
        XCTAssertEqual(none.reachedAny, false)
    }

    func testSmallEvents() throws {
        XCTAssertEqual(try Fixtures.decode(CommandAck.self, "command_ack", "land").command, "land")
        XCTAssertEqual(try Fixtures.decode(StreamVideoState.self, "stream_video_state", "off").enabled, false)
        for c in try Fixtures.cases("error") {
            let e = try JSONDecoder().decode(ErrorPayload.self, from: XCTUnwrap(c.payloadData))
            XCTAssertFalse(try XCTUnwrap(e.message).isEmpty, c.name)
        }
    }

    // MARK: leniency

    func testEmptyObjectDecodesEveryModelToNils() throws {
        let empty = Data("{}".utf8)
        let d = JSONDecoder()
        XCTAssertEqual(try d.decode(StatusPayload.self, from: empty), StatusPayload())
        XCTAssertEqual(try d.decode(FleetStatusPayload.self, from: empty), FleetStatusPayload())
        XCTAssertEqual(try d.decode(DroneRecord.self, from: empty), DroneRecord())
        XCTAssertEqual(try d.decode(FleetEvent.self, from: empty), FleetEvent())
        XCTAssertEqual(try d.decode(FleetPlanPayload.self, from: empty), FleetPlanPayload())
        XCTAssertEqual(try d.decode(FleetDetection.self, from: empty), FleetDetection())
        XCTAssertEqual(try d.decode(FramePayload.self, from: empty), FramePayload())
        XCTAssertEqual(try d.decode(MatchAlert.self, from: empty), MatchAlert())
        XCTAssertEqual(try d.decode(DetectionProfilesPayload.self, from: empty), DetectionProfilesPayload())
        XCTAssertEqual(try d.decode(CeilingSettingsPayload.self, from: empty), CeilingSettingsPayload())
        XCTAssertEqual(try d.decode(ReasoningProviderPayload.self, from: empty), ReasoningProviderPayload())
        XCTAssertEqual(try d.decode(ManualStatusPayload.self, from: empty), ManualStatusPayload())
        XCTAssertEqual(try d.decode(ManualReply.self, from: empty), ManualReply())
        XCTAssertEqual(try d.decode(LandAllAck.self, from: empty), LandAllAck())
        XCTAssertEqual(try d.decode(CommandAck.self, from: empty), CommandAck())
        XCTAssertEqual(try d.decode(StreamVideoState.self, from: empty), StreamVideoState())
        XCTAssertEqual(try d.decode(ErrorPayload.self, from: empty), ErrorPayload())
    }

    func testWrongTypesDecodeToNilInsteadOfFailing() throws {
        let json = #"{"battery":"high","connected":"yes","lost":3,"drone_id":7,"orphan_land_in_s":"120","control_mode":5}"#
        let r = try JSONDecoder().decode(DroneRecord.self, from: Data(json.utf8))
        XCTAssertNil(r.battery)
        XCTAssertNil(r.connected)
        XCTAssertNil(r.lost)
        XCTAssertEqual(r.droneId, "7")
        XCTAssertEqual(r.orphanLandInS, 120)
        XCTAssertEqual(r.controlMode, .unknown(""))
    }

    func testModelsRoundTripThroughEncode() throws {
        let original = try Fixtures.decode(FleetStatusPayload.self, "fleet_status", "one_held_orphan_countdown")
        let again = try JSONDecoder().decode(FleetStatusPayload.self, from: JSONEncoder().encode(original))
        XCTAssertEqual(original, again)
    }

    // MARK: forward compatibility

    /// For each event of `unknown_keys.json`, decoding the record with `x_future_field` added at
    /// every object level equals decoding the original payload.
    func testUnknownKeysDecodeToTheSameValueAsTheOriginal() throws {
        let (meta, cases) = try Fixtures.load("unknown_keys")
        let marker = try XCTUnwrap(meta["unknown_key"] as? String)
        var compared = 0
        for c in cases {
            guard let event = c.event else { continue }
            XCTAssertTrue(GroundEvent.handledEventNames.contains(event), "\(event) not handled")
            let withKeys = try XCTUnwrap(c.payloadData, c.name)
            XCTAssertTrue(String(decoding: withKeys, as: UTF8.self).contains(marker), c.name)
            let sourceFile = try XCTUnwrap(c.raw["source_file"] as? String, c.name)
            let sourceCase = try XCTUnwrap(c.raw["source_case"] as? String, c.name)
            let original = try Fixtures.theCase(sourceFile.replacingOccurrences(of: ".json", with: ""), sourceCase)
            let a = GroundEvent.decode(name: event, payload: withKeys)
            let b = GroundEvent.decode(name: event, payload: try XCTUnwrap(original.payloadData))
            XCTAssertEqual(a, b, "\(c.name) (\(sourceFile)/\(sourceCase))")
            if case .unknown = a { XCTFail("\(c.name) not decoded") }
            if case .undecodable = a { XCTFail("\(c.name) not decodable") }
            compared += 1
        }
        XCTAssertEqual(compared, 16, "one case per event")
    }
}
