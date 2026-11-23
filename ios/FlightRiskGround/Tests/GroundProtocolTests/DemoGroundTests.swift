import XCTest
@testable import GroundProtocol

/// The demo ground: its templates are the contract fixtures, its scenario plays in order, and
/// every command is answered on the phone as the ground would answer it.
final class DemoGroundTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_700_000_000)

    private func makeGround() -> DemoGround {
        DemoGround(startedAt: start, timeZone: TimeZone(identifier: "UTC")!)
    }

    /// Runs the demo in quarter-second steps up to `until` and returns each event with the time it came out.
    private func run(_ ground: inout DemoGround, from: TimeInterval = 0, until: TimeInterval,
                     before: ((TimeInterval, inout DemoGround) -> Void)? = nil) -> [(t: TimeInterval, event: GroundEvent)] {
        var out: [(TimeInterval, GroundEvent)] = []
        var t = from
        while t <= until + 1e-9 {
            before?(t, &ground)
            for e in ground.advance(to: t) { out.append((t, e)) }
            t += 0.25
        }
        return out
    }

    private func name(_ e: GroundEvent) -> String {
        switch e {
        case .status: return "status"
        case .fleetStatus: return "fleet_status"
        case let .fleetEvent(ev): return "fleet_event:" + (ev.resolvedKind?.wire ?? "")
        case .fleetPlan: return "fleet_plan"
        case .fleetDetection: return "fleet_detection"
        case .frame: return "frame"
        case .matchAlert: return "match_alert"
        case .matchAlertReminder: return "match_alert_reminder"
        case .matchAlertResolved: return "match_alert_resolved"
        case .reasoningResult: return "reasoning_result"
        case .alertUpgrade: return "alert_upgrade"
        case .detectionProfiles: return "detection_profiles"
        case .ceilingSettings: return "ceiling_settings"
        case .reasoningProvider: return "reasoning_provider"
        case .manualStatus: return "manual_status"
        case .manualAck: return "manual_ack"
        case .manualResult: return "manual_result"
        case .landAllAck: return "land_all_ack"
        case .commandAck: return "command_ack"
        case .streamVideoState: return "stream_video_state"
        case .error: return "error"
        case .warning: return "warning"
        case let .unknown(n): return "unknown:" + n
        case let .undecodable(n): return "undecodable:" + n
        }
    }

    private func statuses(_ events: [(t: TimeInterval, event: GroundEvent)]) -> [(t: TimeInterval, status: FleetStatusPayload)] {
        events.compactMap { if case let .fleetStatus(p) = $0.event { return ($0.t, p) } else { return nil } }
    }

    private func firstTime(_ events: [(t: TimeInterval, event: GroundEvent)], _ eventName: String) -> TimeInterval? {
        events.first { name($0.event) == eventName }?.t
    }

    // MARK: Fixtures

    /// Each template is the committed fixture case's payload, minus its image keys.
    func testEveryTemplateIsTheCommittedFixture() throws {
        XCTAssertGreaterThanOrEqual(DemoFixtures.all.count, 20)
        for template in DemoFixtures.all {
            let c = try Fixtures.theCase(template.file, template.caseName)
            XCTAssertEqual(c.event, template.event, "\(template.file)/\(template.caseName)")
            var expected = try XCTUnwrap(c.payload as? [String: Any], "\(template.file)/\(template.caseName)")
            for key in template.droppedKeys {
                XCTAssertNotNil(expected.removeValue(forKey: key), "\(template.caseName) has no \(key) to drop")
            }
            let actual = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(template.json.utf8)) as? [String: Any])
            XCTAssertEqual(NSDictionary(dictionary: actual), NSDictionary(dictionary: expected),
                           "\(template.file)/\(template.caseName) drifted from the fixture; copy it again")
            XCTAssertTrue(template.droppedKeys.allSatisfy { ["image", "snapshot", "thumbnail"].contains($0) })
        }
    }

    func testEveryTemplateDecodesToItsTypedEvent() {
        for template in DemoFixtures.all {
            switch template.decoded() {
            case .unknown, .undecodable: XCTFail("\(template.caseName) did not decode")
            default: break
            }
        }
    }

    // MARK: Scenario

    func testTheScenarioPlaysInOrder() throws {
        var g = makeGround()
        let events = run(&g, until: DemoGround.Moment.finished + 2)
        let hello = events.prefix { $0.t == 0 }.map { name($0.event) }
        XCTAssertEqual(hello, ["status", "reasoning_provider", "detection_profiles", "ceiling_settings", "fleet_plan",
                               "fleet_status", "manual_status", "stream_video_state"])
        XCTAssertEqual(firstTime(events, "fleet_event:start"), DemoGround.Moment.start)
        XCTAssertEqual(firstTime(events, "fleet_event:lane_blocked"), DemoGround.Moment.obstacleCut)
        XCTAssertEqual(firstTime(events, "fleet_detection"), DemoGround.Moment.personAlert)
        XCTAssertEqual(firstTime(events, "match_alert"), DemoGround.Moment.personAlert)
        XCTAssertEqual(firstTime(events, "reasoning_result"), DemoGround.Moment.verdict)
        XCTAssertEqual(firstTime(events, "alert_upgrade"), DemoGround.Moment.verdict)
        XCTAssertEqual(firstTime(events, "match_alert_resolved"), DemoGround.Moment.operatorAnswer)
        XCTAssertEqual(firstTime(events, "fleet_event:land_all"), DemoGround.Moment.landAll)
        XCTAssertTrue(g.isFinished)
        XCTAssertTrue(g.receivedCommands.isEmpty)
        // Nothing the decoder could not read, and no reminder (the alert is new).
        XCTAssertFalse(events.contains { name($0.event).hasPrefix("unknown") || name($0.event).hasPrefix("undecodable") })

        // Takeoff, flight and landing, as the fleet status shows them.
        let s = statuses(events)
        let atStart = try XCTUnwrap(s.last { $0.t < DemoGround.Moment.takeoff })
        XCTAssertEqual(atStart.status.drones?["demo-drone-1"]?.isFlying, false)
        let flying = try XCTUnwrap(s.first { $0.t >= DemoGround.Moment.takeoff })
        XCTAssertEqual(flying.status.drones?["demo-drone-1"]?.isFlying, true)
        XCTAssertEqual(flying.status.drones?["demo-drone-2"]?.heightCm, 130, "flies at its plan altitude")
        let last = try XCTUnwrap(s.last)
        XCTAssertEqual(last.status.drones?["demo-drone-1"]?.isFlying, false)
        XCTAssertEqual(last.status.drones?["demo-drone-2"]?.isFlying, false)
        XCTAssertEqual(last.status.drones?["demo-drone-1"]?.heightCm, 0)
        XCTAssertFalse(g.isFlying("demo-drone-1"))
    }

    func testTheDronesMoveThroughTheirLanes() throws {
        var g = makeGround()
        let s = statuses(run(&g, until: DemoGround.Moment.landAll))
        let drone1 = s.compactMap { $0.status.drones?["demo-drone-1"] }
        let lanes = drone1.compactMap(\.laneIndex)
        XCTAssertEqual(lanes.first, 0)
        XCTAssertEqual(lanes, lanes.sorted(), "lanes are flown in order")
        XCTAssertEqual(Set(lanes), [0, 1, 2])
        XCTAssertTrue(drone1.allSatisfy { $0.lanesTotal == nil || $0.lanesTotal == 3 })
        let ys = drone1.compactMap(\.relYCm)
        XCTAssertEqual(ys.max(), 400, "lane 0 reaches the end of the 400 cm zone")
        XCTAssertGreaterThan(Set(ys).count, 10, "the drone moves between statuses")
        let batteries = drone1.compactMap(\.battery)
        XCTAssertLessThan(try XCTUnwrap(batteries.last), try XCTUnwrap(batteries.first))
        // drone-2 is cut on lane 0 and goes on with lane 1.
        let drone2 = s.filter { $0.t > DemoGround.Moment.obstacleCut }.compactMap { $0.status.drones?["demo-drone-2"]?.laneIndex }
        XCTAssertEqual(drone2.first, 1)
    }

    func testTheObstacleCutAndTheAlertCarryTheirFields() throws {
        var g = makeGround()
        let events = run(&g, until: DemoGround.Moment.operatorAnswer)
        guard case let .fleetEvent(cut)? = events.first(where: { name($0.event) == "fleet_event:lane_blocked" })?.event else {
            return XCTFail("no obstacle cut")
        }
        XCTAssertEqual(cut.droneId, "demo-drone-2")
        XCTAssertEqual(cut.laneIndex, 0)
        XCTAssertEqual(cut.xCm, 250)
        XCTAssertEqual(cut.yCm, 210)
        XCTAssertEqual(cut.unflownCm, 190)
        XCTAssertEqual(GroundText.fleetEventBanner(cut), "OBSTACLE demo-drone-2: lane 0 stopped at (250, 210) cm, 190 cm not searched")

        guard case let .matchAlert(alert)? = events.first(where: { name($0.event) == "match_alert" })?.event else {
            return XCTFail("no alert")
        }
        XCTAssertEqual(alert.alertId, DemoGround.alertId)
        XCTAssertTrue(alert.alertId?.hasPrefix(DemoGround.idPrefix) == true)
        XCTAssertEqual(alert.alertLevel, .possibleMatch)
        XCTAssertEqual(alert.gemmaConfidence, .string("pending"))
        XCTAssertEqual(alert.reasoning, "Awaiting Gemma reasoning...")
        XCTAssertEqual(alert.time, "22:13:34", "the start time plus 14 s, in the given time zone")
        XCTAssertNil(alert.snapshot, "the demo carries no images")
        XCTAssertFalse(events.contains { if case let .fleetDetection(d) = $0.event { return d.thumbnail != nil } else { return false } })
        XCTAssertFalse(events.contains { if case let .frame(f) = $0.event { return f.image != nil } else { return false } })
    }

    /// The alert, fed with the rest of the scenario through the same book the Ground tab uses,
    /// ends confirmed with a match verdict and the operator's answer.
    func testTheAlertGetsItsVerdictAndIsRaised() throws {
        var g = makeGround()
        var book = AlertVerdictBook()
        var reducer = DroneStateReducer()
        var alert: MatchAlert?
        var answer: MatchAlertResolved?
        for (_, e) in run(&g, until: DemoGround.Moment.finished) {
            book.apply(e)
            reducer.apply(e)
            if case let .matchAlert(a) = e { alert = a }
            if case let .matchAlertResolved(r) = e { answer = r }
        }
        let a = try XCTUnwrap(alert)
        XCTAssertEqual(book.level(for: a), .confirmedMatch)
        let shown = try XCTUnwrap(book.display(for: a))
        XCTAssertEqual(shown.headline, "AI check: match (high confidence)")
        XCTAssertEqual(shown.rowRationale?.hasPrefix("Simulated verdict for the demo"), true)
        XCTAssertFalse(shown.isNeutral)
        XCTAssertEqual(answer?.alertId, DemoGround.alertId)
        XCTAssertEqual(answer?.action, .confirm)
        XCTAssertEqual(answer?.ok, true)
        XCTAssertEqual(reducer.sortedDroneIds, ["demo-drone-1", "demo-drone-2"])
        XCTAssertEqual(reducer.cards.map(\.control), [.autonomous(.onGround), .autonomous(.onGround)])
    }

    func testTheScenarioIsDeterministic() {
        var a = makeGround()
        var b = makeGround()
        let ea = run(&a, until: 40).map(\.event)
        let eb = run(&b, until: 40).map(\.event)
        XCTAssertEqual(ea, eb)
    }

    func testALongPauseSendsAtMostTwoStatusesButEveryMoment() {
        var g = makeGround()
        _ = g.advance(to: 0)
        let events = g.advance(to: 100)
        XCTAssertLessThanOrEqual(events.filter { if case .fleetStatus = $0 { return true } else { return false } }.count,
                                 2 + 4, "two periodic statuses plus the ones the moments send")
        let names = events.map(name)
        let order = ["fleet_event:start", "fleet_event:lane_blocked", "match_alert", "reasoning_result",
                     "match_alert_resolved", "fleet_event:land_all"]
        let positions = order.compactMap { names.firstIndex(of: $0) }
        XCTAssertEqual(positions.count, order.count)
        XCTAssertEqual(positions, positions.sorted())
        XCTAssertTrue(g.isFinished)
        XCTAssertEqual(g.advance(to: 50), [], "time never goes back")
    }

    // MARK: Commands, answered on the phone

    func testLandAllIsAnsweredAndLandsEveryDrone() throws {
        var g = makeGround()
        let events = run(&g, until: 16) { t, ground in
            if t == 8 { ground.receive(.landAll, at: t) }
        }
        XCTAssertEqual(g.receivedCommands, [.landAll])
        let after = events.filter { $0.t >= 8 }
        guard case let .landAllAck(ack)? = after.first(where: { name($0.event) == "land_all_ack" })?.event else {
            return XCTFail("no land_all_ack")
        }
        XCTAssertEqual(ack.reachedAny, true)
        XCTAssertEqual(GroundText.landAllResult(ack).isFailure, false)
        XCTAssertEqual(firstTime(after, "land_all_ack"), 8 + DemoGround.answerDelay)
        XCTAssertNotNil(firstTime(after, "fleet_event:land_all"))
        let landed = try XCTUnwrap(statuses(events).last)
        XCTAssertEqual(landed.status.drones?.values.map(\.isFlying), [false, false])
        // The drones stay down: no obstacle cut and no alert after a LAND ALL at 8 s.
        XCTAssertNil(firstTime(events, "match_alert"))
        let rest = run(&g, from: 16.25, until: 40)
        XCTAssertNil(firstTime(rest, "match_alert"))
        XCTAssertNil(firstTime(rest, "fleet_event:land_all"))
    }

    func testLandAllBeforeTakeoffKeepsTheDronesOnTheGround() {
        var g = makeGround()
        let events = run(&g, until: 10) { t, ground in
            if t == 1 { ground.receive(.landAll, at: t) }
        }
        XCTAssertTrue(statuses(events).allSatisfy { $0.status.drones?.values.allSatisfy { $0.isFlying == false } == true })
        XCTAssertNotNil(firstTime(events, "land_all_ack"))
    }

    func testLandOneDroneIsAcceptedThenAcknowledged() throws {
        var g = makeGround()
        let events = run(&g, until: 12) { t, ground in
            if t == 7 { ground.receive(.landDrone(droneId: "demo-drone-1"), at: t) }
        }
        let after = events.filter { $0.t >= 7 }
        guard case let .manualAck(ack)? = after.first(where: { name($0.event) == "manual_ack" })?.event,
              case let .manualResult(result)? = after.first(where: { name($0.event) == "manual_result" })?.event else {
            return XCTFail("missing answers")
        }
        XCTAssertEqual(ack.droneId, "demo-drone-1")
        XCTAssertEqual(ack.ok, true)
        XCTAssertEqual(ack.action, .land)
        XCTAssertEqual(result.droneId, "demo-drone-1")
        XCTAssertEqual(result.ok, true)
        XCTAssertEqual(firstTime(after, "manual_ack"), 7 + DemoGround.answerDelay)
        XCTAssertEqual(firstTime(after, "manual_result"), 7 + DemoGround.finalAnswerDelay)
        XCTAssertFalse(g.isFlying("demo-drone-1"))
        XCTAssertTrue(g.isFlying("demo-drone-2"), "only the drone named is landed")
    }

    func testRefusalsAreAnsweredLikeTheGround() throws {
        var g = makeGround()
        _ = g.advance(to: 6)
        g.receive(.landDrone(droneId: "drone-9"), at: 6)
        g.receive(.resumeHeldDrone(droneId: "demo-drone-1"), at: 6)
        let answers = g.advance(to: 7).compactMap { e -> ManualReply? in
            if case let .manualAck(r) = e { return r }
            return nil
        }
        XCTAssertEqual(answers.count, 2)
        XCTAssertEqual(answers[0].droneId, "drone-9")
        XCTAssertEqual(answers[0].ok, false)
        XCTAssertEqual(answers[0].reason, .invalid)
        XCTAssertEqual(answers[0].detail, "'drone-9' is not in the fleet")
        XCTAssertEqual(answers[1].action, .resumeSearch)
        XCTAssertEqual(answers[1].ok, false)
        XCTAssertEqual(answers[1].reason, .notManual)
        XCTAssertEqual(GroundText.manualRefusal(answers[1]), "demo-drone-1: demo-drone-1 is not under manual control")
    }

    func testProfileChangesOnTheGroundAndRefusedInFlight() throws {
        func profiles(_ events: [GroundEvent]) -> DetectionProfilesPayload? {
            events.reversed().compactMap { if case let .detectionProfiles(p) = $0 { return p } else { return nil } }.first
        }
        var g = makeGround()
        _ = g.advance(to: 1)
        g.receive(.setDetectionProfile(droneId: "demo-drone-1", profile: "top_down"), at: 1)
        let onGround = try XCTUnwrap(profiles(g.advance(to: 2))?.drones?["demo-drone-1"])
        XCTAssertEqual(onGround.active, "top_down")
        XCTAssertEqual(onGround.override, "top_down")
        XCTAssertNil(onGround.refusal)
        XCTAssertEqual(onGround.mismatch, false)

        _ = g.advance(to: 8)
        g.receive(.setDetectionProfile(droneId: "demo-drone-1", profile: "level"), at: 8)
        let inFlight = try XCTUnwrap(profiles(g.advance(to: 9))?.drones?["demo-drone-1"])
        XCTAssertEqual(inFlight.active, "top_down")
        XCTAssertEqual(inFlight.desired, "level")
        XCTAssertEqual(inFlight.mismatch, true)
        XCTAssertEqual(inFlight.refusal?.reason, .inFlight)

        g.receive(.setFleetDetectionProfile(profile: "level"), at: 9)
        XCTAssertEqual(profiles(g.advance(to: 10))?.fleetDefault, "level")
        // An unknown profile changes nothing.
        g.receive(.setFleetDetectionProfile(profile: "no_such_profile"), at: 10)
        XCTAssertEqual(profiles(g.advance(to: 11))?.fleetDefault, "level")
    }

    func testStateRequestsAreAnswered() {
        var g = makeGround()
        _ = g.advance(to: 4)
        for command in GroundCommand.stateRequests { g.receive(command, at: 4) }
        let names = g.advance(to: 4 + DemoGround.stateAnswerDelay).map(name)
        XCTAssertEqual(names, ["fleet_status", "fleet_plan", "detection_profiles", "ceiling_settings",
                               "reasoning_provider", "manual_status"])
        XCTAssertEqual(g.receivedCommands, GroundCommand.stateRequests)
    }

    func testTheManualStatusNamesEveryDroneAndNobodyInControl() throws {
        var g = makeGround()
        guard case let .manualStatus(m)? = g.advance(to: 0).first(where: { name($0) == "manual_status" }) else {
            return XCTFail("no manual_status")
        }
        XCTAssertNil(m.controlled)
        XCTAssertEqual(m.drones?.keys.sorted(), ["demo-drone-1", "demo-drone-2"])
        XCTAssertEqual(m.drones?["demo-drone-2"]?.controlMode, .autonomous)
    }

    // MARK: Policy and placeholder

    func testTheDemoNeverStartsBesideALiveLink() {
        XCTAssertEqual(DemoModePolicy.startStep(liveLinkOpen: false, liveLinkSwitchedOn: false), .startNow)
        XCTAssertEqual(DemoModePolicy.startStep(liveLinkOpen: true, liveLinkSwitchedOn: true), .confirmDisconnectFirst)
        XCTAssertEqual(DemoModePolicy.startStep(liveLinkOpen: false, liveLinkSwitchedOn: true), .confirmDisconnectFirst)
        XCTAssertEqual(DemoModePolicy.startStep(liveLinkOpen: true, liveLinkSwitchedOn: false), .confirmDisconnectFirst)
    }

    /// Live drones in the air, or a LAND ALL that is unanswered or failed, refuse the demo with
    /// the reason, whatever the link does: starting it would close the link they depend on.
    func testTheDemoIsRefusedWhileLiveDronesFlyOrALandAllIsOpen() {
        XCTAssertEqual(DemoModePolicy.startStep(liveLinkOpen: true, liveLinkSwitchedOn: true, flyingDrones: 2),
                       .refused("2 drones are in the air. Land them first. The demo closes the ground link, and LAND ALL on this phone would then reach only the simulated drones."))
        XCTAssertEqual(DemoModePolicy.startStep(liveLinkOpen: false, liveLinkSwitchedOn: false, flyingDrones: 1, flyingStateIsStale: true),
                       .refused("1 drone was in the air at the last update from the ground. Land it first. The demo closes the ground link, and LAND ALL on this phone would then reach only the simulated drones."))
        XCTAssertEqual(DemoModePolicy.startStep(liveLinkOpen: true, liveLinkSwitchedOn: true, landAll: .unanswered),
                       .refused(DemoModePolicy.landAllUnansweredRefusal))
        XCTAssertEqual(DemoModePolicy.startStep(liveLinkOpen: false, liveLinkSwitchedOn: false, landAll: .failed),
                       .refused(DemoModePolicy.landAllFailedRefusal))
        // Flying drones are named first: they are the reason that matters most.
        XCTAssertEqual(DemoModePolicy.startStep(liveLinkOpen: true, liveLinkSwitchedOn: true, flyingDrones: 3, landAll: .failed),
                       .refused(DemoModePolicy.flyingRefusal(3, stale: false)))
        XCTAssertEqual(DemoModePolicy.startStep(liveLinkOpen: true, liveLinkSwitchedOn: true, flyingDrones: 0, landAll: .settled), .confirmDisconnectFirst)
    }

    // MARK: Ids

    /// Every id the demo sends starts with "demo-": the drones, the alert and its track, in every
    /// event of the scenario and in every answer to a command.
    func testEveryIdTheDemoSendsCarriesTheDemoPrefix() throws {
        var g = makeGround()
        var events = run(&g, until: DemoGround.Moment.finished + 2).map(\.event)
        // Then every command, answered by the demo.
        let t = g.now
        for command: GroundCommand in [
            .landAll, .landDrone(droneId: DemoGround.spotterDroneId), .resumeHeldDrone(droneId: DemoGround.cutDroneId),
            .setDetectionProfile(droneId: DemoGround.spotterDroneId, profile: "top_down"),
        ] + GroundCommand.stateRequests {
            g.receive(command, at: t)
        }
        events += g.advance(to: t + 5)
        var ids: [String] = []
        var lockedReasons: [String] = []
        for e in events {
            switch e {
            case let .fleetStatus(p):
                for (key, r) in p.drones ?? [:] { ids += [key] + [r.droneId].compactMap { $0 } }
            case let .fleetPlan(p):
                ids += (p.zones ?? []).compactMap(\.droneId)
            case let .detectionProfiles(p):
                for (key, d) in p.drones ?? [:] { ids += [key] + [d.droneId].compactMap { $0 } }
            case let .ceilingSettings(p):
                ids += (p.drones ?? []).compactMap(\.droneId)
                if let r = p.lockedReason { lockedReasons.append(r) }
            case let .manualStatus(p):
                ids += (p.drones ?? [:]).keys
                if let c = p.controlled { ids.append(c) }
            case let .frame(f):
                ids += [f.droneId].compactMap { $0 }
            case let .fleetDetection(d):
                ids += [d.droneId].compactMap { $0 } + (d.droneIds ?? [])
            case let .fleetEvent(ev):
                ids += [ev.droneId].compactMap { $0 }
            case let .matchAlert(a):
                ids += [a.alertId, a.trackId].compactMap { $0 }
            case let .reasoningResult(r):
                ids += [r.alertId, r.trackId].compactMap { $0 }
            case let .alertUpgrade(u):
                ids += [u.alertId, u.trackId].compactMap { $0 }
            case let .matchAlertResolved(r):
                ids += [r.alertId].compactMap { $0 }
            case let .manualAck(r), let .manualResult(r):
                ids += [r.droneId].compactMap { $0 }
            default:
                break
            }
        }
        XCTAssertGreaterThan(ids.count, 50)
        for id in ids { XCTAssertTrue(id.hasPrefix(DemoGround.idPrefix), "\(id) has no demo prefix") }
        XCTAssertEqual(Set(ids.filter { $0.contains("drone") }), ["demo-drone-1", "demo-drone-2"])
        XCTAssertTrue(ids.contains("demo-alert-1"))
        XCTAssertTrue(ids.contains("demo-track-1"))
        XCTAssertFalse(lockedReasons.isEmpty)
        for r in lockedReasons {
            XCTAssertTrue(r.contains("(demo-drone-1 reported flying)"), r)
        }
        XCTAssertEqual(g.droneIds, ["demo-drone-1", "demo-drone-2"])
    }

    func testThePlaceholderIsAFlatDrawingNotAPhoto() throws {
        let image = try XCTUnwrap(DemoPlaceholder.makeImage())
        XCTAssertEqual(image.width, DemoPlaceholder.defaultSize)
        XCTAssertEqual(image.height, DemoPlaceholder.defaultSize)
        let pixels = try rgba(image)
        var colours = Set<[UInt8]>()
        for i in stride(from: 0, to: pixels.count, by: 4) { colours.insert(Array(pixels[i..<i + 4])) }
        XCTAssertEqual(colours, [DemoPlaceholder.backgroundRGBA, DemoPlaceholder.figureRGBA], "two flat colours, nothing photographic")
        let figure = pixels.count / 4 - (0..<(pixels.count / 4)).filter { Array(pixels[$0 * 4..<$0 * 4 + 4]) == DemoPlaceholder.backgroundRGBA }.count
        XCTAssertGreaterThan(Double(figure) / Double(pixels.count / 4), 0.15, "the figure is visible")
        XCTAssertEqual(try rgba(try XCTUnwrap(DemoPlaceholder.makeImage())), pixels, "the same every time")
        XCTAssertNil(DemoPlaceholder.makeImage(size: 8))
    }

    private func rgba(_ image: CGImage) throws -> [UInt8] {
        let w = image.width, h = image.height
        var bytes = [UInt8](repeating: 0, count: w * h * 4)
        let space = try XCTUnwrap(CGColorSpace(name: CGColorSpace.sRGB))
        let drawn: Bool = bytes.withUnsafeMutableBytes { buffer in
            guard let ctx = CGContext(data: buffer.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                      space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
            return true
        }
        XCTAssertTrue(drawn)
        return bytes
    }
}
