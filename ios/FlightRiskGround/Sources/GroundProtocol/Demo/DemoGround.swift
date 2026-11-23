import Foundation

/// A scripted FlightRisk ground station that runs on the phone, for the demo mode (App Store
/// review and trying the app without drones).
///
/// It replays the contract fixtures (`DemoFixtures`) as a timed scenario and moves two simulated
/// drones through the lanes of the fixture plan:
///
/// | Time (s) | What happens |
/// |---|---|
/// | 0 | Connect: `status`, reasoning provider, detection profiles, ceiling, plan, fleet status. |
/// | 3 | `fleet_event` `start`. |
/// | 5 | Both drones take off and fly their lanes at 30 cm/s. |
/// | 12 | demo-drone-2 hits an obstacle: `fleet_event` `lane_blocked`; it skips to its next lane. |
/// | 14 | demo-drone-1 sees a person: `fleet_detection` and a `match_alert` waiting for the AI check. |
/// | 22 | The AI check answers: `reasoning_result` (match, high confidence) and `alert_upgrade`. |
/// | 30 | The operator confirms the alert on the laptop: `match_alert_resolved`. |
/// | 33 | The ground lands every drone: `fleet_event` `land_all`; landing takes 3 s. |
/// | 37 | The scenario is finished; the drones stay on the ground. |
///
/// `fleet_status` (and a detections-only `frame` per flying drone) is sent every second, like a
/// live ground. Every command the phone sends is answered here, on the phone, as the ground would
/// answer it (`receive(_:at:)`): nothing is ever sent to a network. The type is a pure value: time
/// moves only through `advance(to:)`, so a test can drive it second by second.
///
/// Every id the demo sends starts with `idPrefix`: the drones are `demo-drone-1` and
/// `demo-drone-2` (the fixtures' `drone-1` and `drone-2`, renamed as they are loaded), the alert
/// is `demo-alert-1` and its track `demo-track-1`. A demo drone can never share an id with a real
/// one.
public struct DemoGround: Sendable {
    /// When each part of the scenario happens, in seconds from the start.
    public enum Moment {
        public static let start: TimeInterval = 3
        public static let takeoff: TimeInterval = 5
        public static let obstacleCut: TimeInterval = 12
        public static let personAlert: TimeInterval = 14
        public static let verdict: TimeInterval = 22
        public static let operatorAnswer: TimeInterval = 30
        public static let landAll: TimeInterval = 33
        public static let finished: TimeInterval = 37
    }

    public static let statusInterval: TimeInterval = 1
    public static let speedCmPerSecond: Double = 30
    public static let landingSeconds: TimeInterval = 3
    /// How long the demo ground takes to answer a command (`manual_ack`, `land_all_ack`).
    public static let answerDelay: TimeInterval = 0.5
    /// How long until the drone's edge "answers" a land (`manual_result`).
    public static let finalAnswerDelay: TimeInterval = 1.5
    public static let profileAnswerDelay: TimeInterval = 0.8
    public static let stateAnswerDelay: TimeInterval = 0.2

    /// Every id the demo sends (drones, the alert, its track) carries this prefix, so it can
    /// never be taken for a real ground's.
    public static let idPrefix = "demo-"
    public static let alertId = "demo-alert-1"
    public static let trackId = "demo-track-1"
    public static let cutDroneId = "demo-drone-2"
    public static let spotterDroneId = "demo-drone-1"

    /// The demo id of a drone the contract fixtures name: "drone-1" becomes "demo-drone-1".
    public static func demoId(_ fixtureId: String) -> String {
        fixtureId.hasPrefix(idPrefix) ? fixtureId : idPrefix + fixtureId
    }
    public static let verdictRationale =
        "Simulated verdict for the demo: the build and the clothing colours of the person match the reference placeholder. Verify in person before acting."

    private enum Beat: Sendable {
        case hello, start, takeoff, obstacleCut, personAlert, verdict, operatorAnswer, landAll
    }

    private static let script: [(at: TimeInterval, beat: Beat)] = [
        (0, .hello), (Moment.start, .start), (Moment.takeoff, .takeoff), (Moment.obstacleCut, .obstacleCut),
        (Moment.personAlert, .personAlert), (Moment.verdict, .verdict), (Moment.operatorAnswer, .operatorAnswer),
        (Moment.landAll, .landAll),
    ]

    /// A command's answer, due at a time.
    private enum Work: Sendable {
        case landAllAnswer
        case landAnswer(String)
        case landResult(String)
        case landUnknownDrone(String)
        case resumeAnswer(String)
        case profile(droneId: String?, profile: String)
        case state(GroundCommand)
    }

    private struct Pending: Sendable {
        let due: TimeInterval
        let seq: Int
        let work: Work
    }

    private struct Leg: Sendable {
        let dx: Double
        let dy: Double
        let length: Double
        let heading: Double
    }

    private struct Drone: Sendable {
        let id: String
        let template: DroneRecord
        let originX: Double
        let originY: Double
        let altCm: Double
        let legs: [Leg]
        var battery: Double
        var flying = false
        var landed = false
        var progressCm: Double = 0
        var landingSince: TimeInterval?

        var pathLength: Double { legs.reduce(0) { $0 + $1.length } }

        /// The leg being flown, 0-based, or `legs.count` once every lane is done.
        var legIndex: Int {
            var start = 0.0
            for (i, leg) in legs.enumerated() {
                if progressCm < start + leg.length { return i }
                start += leg.length
            }
            return legs.count
        }

        var position: (x: Double, y: Double, heading: Double) {
            var x = originX, y = originY, heading = legs.first?.heading ?? 0
            var left = progressCm
            for leg in legs {
                let step = min(left, leg.length)
                x += leg.dx * step
                y += leg.dy * step
                heading = leg.heading
                left -= step
                if left <= 0 { break }
            }
            return (x, y, heading)
        }
    }

    // MARK: State

    /// Seconds since the demo started, as of the last `advance(to:)`.
    public private(set) var now: TimeInterval = 0
    /// Every command the phone sent to the demo, oldest first (at most 200 kept).
    public private(set) var receivedCommands: [GroundCommand] = []
    /// True while the scenario's person alert has been raised.
    public private(set) var alertRaised = false

    private var scriptIndex = 0
    private var nextStatusAt: TimeInterval = 0
    private var pending: [Pending] = []
    private var sequence = 0
    private var started = false
    private var ceilingInFlight = false
    private var drones: [Drone]
    private var profiles: DetectionProfilesPayload
    private let alertTimeText: String

    /// - Parameters:
    ///   - startedAt: the wall-clock time the demo starts; only used for the alert's time text.
    ///   - timeZone: the time zone of that text.
    public init(startedAt: Date = Date(), timeZone: TimeZone = .current) {
        let plan = Self.fleetPlanTemplate
        let records = Self.fleetStatusTemplate.drones ?? [:]
        drones = (plan.zones ?? []).compactMap { zone in
            guard let id = zone.droneId else { return nil }  // Already a demo id.
            let legs: [Leg] = (zone.lanes ?? []).compactMap { lane in
                guard let length = lane.distanceCm, length > 0 else { return nil }
                switch lane.direction {
                case .forward?: return Leg(dx: 0, dy: 1, length: length, heading: 0)
                case .right?: return Leg(dx: 1, dy: 0, length: length, heading: 90)
                case .back?: return Leg(dx: 0, dy: -1, length: length, heading: 180)
                case .left?: return Leg(dx: -1, dy: 0, length: length, heading: 270)
                default: return nil
                }
            }
            let template = records[id] ?? DroneRecord()
            return Drone(id: id, template: template, originX: zone.xCm ?? 0, originY: zone.yCm ?? 0,
                         altCm: zone.altCm ?? 100, legs: legs, battery: template.battery ?? 87)
        }.sorted { $0.id < $1.id }
        profiles = Self.profilesTemplate
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = timeZone
        formatter.dateFormat = "HH:mm:ss"
        alertTimeText = formatter.string(from: startedAt.addingTimeInterval(Moment.personAlert))
    }

    /// True once the scripted scenario has played to its end.
    public var isFinished: Bool { now >= Moment.finished }

    /// The ids of the simulated drones, sorted.
    public var droneIds: [String] { drones.map(\.id) }

    /// True while the drone is in the air (landing included).
    public func isFlying(_ id: String) -> Bool { drones.first { $0.id == id }?.flying == true }

    // MARK: Time

    /// Moves the demo clock to `elapsed` seconds and returns every event due by then, in order:
    /// the scripted moments, the answers to commands, and the periodic status. A large step (the
    /// app was in the background) plays the scenario in order but sends at most two periodic
    /// statuses.
    public mutating func advance(to elapsed: TimeInterval) -> [GroundEvent] {
        guard elapsed.isFinite, elapsed >= now else { return [] }
        var out: [GroundEvent] = []
        while true {
            let beatAt = scriptIndex < Self.script.count ? Self.script[scriptIndex].at : .infinity
            let workAt = pending.first?.due ?? .infinity
            let t = min(beatAt, workAt, nextStatusAt)
            guard t <= elapsed else { break }
            move(to: t)
            if beatAt == t {
                let beat = Self.script[scriptIndex].beat
                scriptIndex += 1
                out += play(beat)
            } else if workAt == t {
                out += perform(pending.removeFirst().work)
            } else {
                out += statusTick()
                nextStatusAt = max(t + Self.statusInterval, elapsed)
            }
        }
        move(to: elapsed)
        return out
    }

    // MARK: Commands

    /// Answers one command, on the phone, as the ground would. The answer is due a moment later
    /// and comes out of `advance(to:)`. Nothing is sent anywhere.
    public mutating func receive(_ command: GroundCommand, at elapsed: TimeInterval) {
        receivedCommands.append(command)
        if receivedCommands.count > 200 { receivedCommands.removeFirst(receivedCommands.count - 200) }
        let base = elapsed.isFinite ? max(now, elapsed) : now
        switch command {
        case .landAll:
            schedule(base + Self.answerDelay, .landAllAnswer)
        case let .landDrone(id):
            if drones.contains(where: { $0.id == id }) {
                schedule(base + Self.answerDelay, .landAnswer(id))
                schedule(base + Self.finalAnswerDelay, .landResult(id))
            } else {
                schedule(base + Self.answerDelay, .landUnknownDrone(id))
            }
        case let .resumeHeldDrone(id):
            schedule(base + Self.answerDelay, .resumeAnswer(id))
        case let .setDetectionProfile(id, profile):
            schedule(base + Self.profileAnswerDelay, .profile(droneId: id, profile: profile))
        case let .setFleetDetectionProfile(profile):
            schedule(base + Self.profileAnswerDelay, .profile(droneId: nil, profile: profile))
        case .getFleetStatus, .getDetectionProfiles, .getCeilingSettings, .getReasoningProvider, .getManualStatus:
            schedule(base + Self.stateAnswerDelay, .state(command))
        }
    }

    private mutating func schedule(_ due: TimeInterval, _ work: Work) {
        sequence += 1
        let item = Pending(due: due, seq: sequence, work: work)
        let index = pending.firstIndex { $0.due > due } ?? pending.endIndex
        pending.insert(item, at: index)
    }

    // MARK: Simulation

    private mutating func move(to t: TimeInterval) {
        let dt = max(0, t - now)
        for i in drones.indices {
            if let since = drones[i].landingSince {
                if t - since >= Self.landingSeconds {
                    drones[i].flying = false
                    drones[i].landed = true
                    drones[i].landingSince = nil
                }
            } else if drones[i].flying {
                drones[i].progressCm = min(drones[i].pathLength, drones[i].progressCm + Self.speedCmPerSecond * dt)
                drones[i].battery = max(0, drones[i].battery - 0.2 * dt)
            }
        }
        now = max(now, t)
    }

    private mutating func startLanding(_ ids: [String]) {
        for i in drones.indices where ids.contains(drones[i].id) {
            if drones[i].flying, drones[i].landingSince == nil { drones[i].landingSince = now }
            if !drones[i].flying { drones[i].landed = true }
        }
    }

    private func height(_ d: Drone) -> Double {
        guard d.flying else { return 0 }
        guard let since = d.landingSince else { return d.altCm }
        let left = max(0, 1 - (now - since) / Self.landingSeconds)
        return (d.altCm * left).rounded()
    }

    private func record(_ d: Drone) -> DroneRecord {
        var r = d.template
        r.droneId = d.id
        r.isFlying = d.flying
        r.battery = d.battery.rounded()
        r.heightCm = height(d)
        let p = d.position
        r.relXCm = p.x
        r.relYCm = p.y
        r.headingDeg = p.heading
        if started {
            r.acked = true
            r.lanesTotal = d.legs.count
            r.laneIndex = d.legIndex
        }
        return r
    }

    // MARK: Payloads

    private func fleetStatus() -> GroundEvent {
        var p = Self.fleetStatusTemplate
        var map: [String: DroneRecord] = [:]
        for d in drones { map[d.id] = record(d) }
        p.drones = map
        return .fleetStatus(p)
    }

    private func manualStatus() -> GroundEvent {
        var p = Self.manualStatusTemplate
        let entry = p.drones?[Self.spotterDroneId] ?? ManualDroneEntry()
        var map: [String: ManualDroneEntry] = [:]
        for d in drones { map[d.id] = entry }
        p.drones = map
        p.controlled = nil
        return .manualStatus(p)
    }

    private func ceiling() -> GroundEvent {
        .ceilingSettings(ceilingInFlight ? Self.ceilingInFlightTemplate : Self.ceilingOnGroundTemplate)
    }

    private func frame(for d: Drone) -> GroundEvent {
        let withMatch = d.id == Self.spotterDroneId && alertRaised && now < Moment.verdict + 4
        var f = withMatch ? Self.frameWithMatchTemplate : Self.frameDetectionsOnlyTemplate
        f.droneId = d.id
        f.image = nil
        f.streamVideo = false
        f.fps = 4
        return .frame(f)
    }

    private mutating func setProfilesFlying() {
        guard var states = profiles.drones else { return }
        for (id, var s) in states {
            s.flying = isFlying(id)
            states[id] = s
        }
        profiles.drones = states
    }

    private mutating func statusTick() -> [GroundEvent] {
        var out = [fleetStatus()]
        for d in drones where d.flying && d.landingSince == nil { out.append(frame(for: d)) }
        if ceilingInFlight, !drones.contains(where: \.flying) {
            // Every drone is down: the ceiling unlocks and the edges report that they are on the ground.
            ceilingInFlight = false
            setProfilesFlying()
            out += [ceiling(), .detectionProfiles(profiles)]
        }
        return out
    }

    // MARK: Scenario

    private mutating func play(_ beat: Beat) -> [GroundEvent] {
        switch beat {
        case .hello:
            nextStatusAt = Self.statusInterval
            return [
                DemoFixtures.status.decoded(), DemoFixtures.reasoningProvider.decoded(), .detectionProfiles(profiles),
                ceiling(), .fleetPlan(Self.fleetPlanTemplate), fleetStatus(), manualStatus(), DemoFixtures.streamVideoOff.decoded(),
            ]
        case .start:
            started = true
            return [DemoFixtures.start.decoded(), fleetStatus()]
        case .takeoff:
            var any = false
            for i in drones.indices where !drones[i].landed && !drones[i].flying {
                drones[i].flying = true
                any = true
            }
            guard any else { return [] }
            ceilingInFlight = true
            setProfilesFlying()
            return [ceiling(), .detectionProfiles(profiles), fleetStatus()]
        case .obstacleCut:
            guard let i = drones.firstIndex(where: { $0.id == Self.cutDroneId }), drones[i].flying,
                  drones[i].landingSince == nil, drones[i].legIndex == 0, let leg = drones[i].legs.first else { return [] }
            let p = drones[i].position
            let unflown = (leg.length - drones[i].progressCm).rounded()
            drones[i].progressCm = leg.length
            let detail = "\(Self.cutDroneId): lane 0 stopped at (\(GroundText.number(p.x)), \(GroundText.number(p.y))) cm, \(GroundText.number(unflown)) cm not searched (obstacle)"
            let event = Self.decode("fleet_event", [
                "event": "lane_blocked", "drone_id": Self.cutDroneId, "lane_index": 0,
                "x_cm": p.x, "y_cm": p.y, "unflown_cm": unflown, "reason": "obstacle", "detail": detail,
            ])
            return [event, fleetStatus()]
        case .personAlert:
            guard let d = drones.first(where: { $0.id == Self.spotterDroneId }), d.flying, d.landingSince == nil else { return [] }
            alertRaised = true
            let p = d.position
            var detection = Self.detectionTemplate
            detection.id = 1
            detection.droneId = d.id
            detection.droneIds = [d.id]
            detection.xCm = p.x
            detection.yCm = p.y
            detection.relXCm = p.x
            detection.relYCm = p.y
            detection.thumbnail = nil
            return [.fleetDetection(detection), .matchAlert(pendingAlert()), frame(for: d)]
        case .verdict:
            guard alertRaised else { return [] }
            let result = Self.decode("reasoning_result", [
                "track_id": Self.trackId, "alert_id": Self.alertId, "type": "analyze",
                "result": ["match": true, "confidence": "high", "reasoning": Self.verdictRationale],
            ])
            let upgrade = Self.decode("alert_upgrade", [
                "track_id": Self.trackId, "alert_id": Self.alertId,
                "new_level": "confirmed_match", "reasoning": Self.verdictRationale,
            ])
            return [result, upgrade]
        case .operatorAnswer:
            guard alertRaised else { return [] }
            var answer = Self.alertConfirmedTemplate
            answer.alertId = Self.alertId
            return [.matchAlertResolved(answer)]
        case .landAll:
            let flying = drones.filter { $0.flying && $0.landingSince == nil }.map(\.id)
            guard !flying.isEmpty else { return [] }
            startLanding(flying)
            return [DemoFixtures.landAllEvent.decoded(), fleetStatus()]
        }
    }

    /// The alert as the ground first sends it: waiting for the AI check (`gemma_confidence`
    /// "pending", `app.py` `_emit_edge_alert`).
    private func pendingAlert() -> MatchAlert {
        var a = Self.alertTemplate
        a.alertId = Self.alertId
        a.trackId = Self.trackId
        a.time = alertTimeText
        a.snapshot = nil
        a.gemmaConfidence = .string("pending")
        a.gemmaMatch = nil
        let label = Self.reasoningProviderTemplate.label ?? "reasoning"
        a.reasoning = "Awaiting \(label) reasoning..."
        return a
    }

    private mutating func perform(_ work: Work) -> [GroundEvent] {
        switch work {
        case .landAllAnswer:
            let flying = drones.filter { $0.flying }.map(\.id)
            startLanding(drones.map(\.id))
            var out = [DemoFixtures.landAllAck.decoded()]
            if !flying.isEmpty { out.append(DemoFixtures.landAllEvent.decoded()) }
            out.append(fleetStatus())
            return out
        case let .landAnswer(id):
            var ack = Self.landAckTemplate
            ack.droneId = id
            return [.manualAck(ack)]
        case let .landResult(id):
            startLanding([id])
            var result = Self.landResultTemplate
            result.droneId = id
            return [.manualResult(result), fleetStatus()]
        case let .landUnknownDrone(id):
            var ack = Self.landUnknownDroneTemplate
            ack.droneId = id
            ack.detail = "'\(id)' is not in the fleet"
            return [.manualAck(ack)]
        case let .resumeAnswer(id):
            var ack = Self.resumeNotManualTemplate
            ack.droneId = id
            ack.detail = "\(id) is not under manual control"
            return [.manualAck(ack)]
        case let .profile(droneId, profile):
            selectProfile(droneId: droneId, profile: profile)
            return [.detectionProfiles(profiles)]
        case let .state(command):
            switch command {
            case .getFleetStatus: return [fleetStatus(), .fleetPlan(Self.fleetPlanTemplate)]
            case .getDetectionProfiles: return [.detectionProfiles(profiles)]
            case .getCeilingSettings: return [ceiling()]
            case .getReasoningProvider: return [DemoFixtures.reasoningProvider.decoded()]
            case .getManualStatus: return [manualStatus()]
            default: return []
            }
        }
    }

    /// A profile choice, as the ground and an edge settle it: on the ground the edge switches; in
    /// flight it refuses (`in_flight`) and the choice waits for the ground.
    private mutating func selectProfile(droneId: String?, profile: String) {
        let known = Set((profiles.profiles ?? []).compactMap(\.name))
        guard profile.isEmpty || known.contains(profile) else { return }
        var states = profiles.drones ?? [:]
        if let droneId {
            guard var s = states[droneId], s.supported == true else { return }
            s.override = profile.isEmpty ? nil : profile
            states[droneId] = settle(s, desired: s.override ?? profiles.fleetDefault)
        } else {
            profiles.fleetDefault = profile.isEmpty ? nil : profile
            for (id, s) in states where s.supported == true && s.override == nil {
                states[id] = settle(s, desired: profiles.fleetDefault)
            }
        }
        profiles.drones = states
    }

    private func settle(_ state: DroneProfileState, desired: String?) -> DroneProfileState {
        var s = state
        let target = desired ?? "default"
        s.desired = desired
        s.requested = desired
        s.pending = false
        s.note = nil
        if isFlying(s.droneId ?? "") && target != s.active {
            s.refusal = Self.inFlightRefusal
            s.mismatch = true
        } else {
            s.active = target
            s.refusal = nil
            s.mismatch = false
        }
        return s
    }

    // MARK: Templates

    private static func decode(_ name: String, _ object: [String: Any]) -> GroundEvent {
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else {
            return .undecodable(name: name)
        }
        return GroundEvent.decode(name: name, payload: data)
    }

    // The templates carry the demo ids: every drone id of a fixture is renamed with `demoId`.

    /// A per-drone map with its keys renamed to demo ids, and `edit` applied to each value.
    private static func renamed<Value>(_ map: [String: Value]?, _ edit: (inout Value, String) -> Void = { _, _ in }) -> [String: Value]? {
        guard let map else { return nil }
        var out: [String: Value] = [:]
        for (fixtureId, value) in map {
            let id = demoId(fixtureId)
            var copy = value
            edit(&copy, id)
            out[id] = copy
        }
        return out
    }

    static var fleetPlanTemplate: FleetPlanPayload {
        guard case var .fleetPlan(p) = DemoFixtures.fleetPlan.decoded() else { return FleetPlanPayload() }
        p.zones = p.zones?.map { zone in
            var z = zone
            z.droneId = z.droneId.map(demoId)
            return z
        }
        return p
    }

    static var fleetStatusTemplate: FleetStatusPayload {
        guard case var .fleetStatus(p) = DemoFixtures.fleetStatus.decoded() else { return FleetStatusPayload() }
        p.drones = renamed(p.drones) { record, id in record.droneId = id }
        return p
    }

    static var profilesTemplate: DetectionProfilesPayload {
        guard case var .detectionProfiles(p) = DemoFixtures.profiles.decoded() else { return DetectionProfilesPayload() }
        p.drones = renamed(p.drones) { state, id in state.droneId = id }
        return p
    }

    static var inFlightRefusal: ProfileRefusal? {
        if case let .detectionProfiles(p) = DemoFixtures.profilesRefused.decoded() {
            return p.drones?.values.first?.refusal
        }
        return nil
    }

    static var ceilingOnGroundTemplate: CeilingSettingsPayload {
        demoCeiling(DemoFixtures.ceilingOnGround)
    }

    static var ceilingInFlightTemplate: CeilingSettingsPayload {
        demoCeiling(DemoFixtures.ceilingInFlight)
    }

    private static func demoCeiling(_ template: DemoFixtures.Template) -> CeilingSettingsPayload {
        guard case var .ceilingSettings(p) = template.decoded() else { return CeilingSettingsPayload() }
        p.drones = p.drones?.map { drone in
            var d = drone
            d.droneId = d.droneId.map(demoId)
            return d
        }
        // "... (drone-1 reported flying)" names a fixture drone.
        p.lockedReason = p.lockedReason?.replacingOccurrences(of: "drone-", with: idPrefix + "drone-")
        return p
    }

    static var reasoningProviderTemplate: ReasoningProviderPayload {
        if case let .reasoningProvider(p) = DemoFixtures.reasoningProvider.decoded() { return p }
        return ReasoningProviderPayload()
    }

    static var manualStatusTemplate: ManualStatusPayload {
        guard case var .manualStatus(p) = DemoFixtures.manualStatus.decoded() else { return ManualStatusPayload() }
        p.drones = renamed(p.drones)
        p.controlled = p.controlled.map(demoId)
        return p
    }

    static var frameDetectionsOnlyTemplate: FramePayload {
        if case let .frame(p) = DemoFixtures.frameDetectionsOnly.decoded() { return p }
        return FramePayload()
    }

    static var frameWithMatchTemplate: FramePayload {
        if case let .frame(p) = DemoFixtures.frameWithMatch.decoded() { return p }
        return FramePayload()
    }

    static var detectionTemplate: FleetDetection {
        if case let .fleetDetection(p) = DemoFixtures.detection.decoded() { return p }
        return FleetDetection()
    }

    static var alertTemplate: MatchAlert {
        if case let .matchAlert(p) = DemoFixtures.alert.decoded() { return p }
        return MatchAlert()
    }

    static var alertConfirmedTemplate: MatchAlertResolved {
        if case let .matchAlertResolved(p) = DemoFixtures.alertConfirmed.decoded() { return p }
        return MatchAlertResolved()
    }

    static var landAckTemplate: ManualReply {
        if case let .manualAck(p) = DemoFixtures.landAck.decoded() { return p }
        return ManualReply()
    }

    static var landUnknownDroneTemplate: ManualReply {
        if case let .manualAck(p) = DemoFixtures.landUnknownDrone.decoded() { return p }
        return ManualReply()
    }

    static var resumeNotManualTemplate: ManualReply {
        if case let .manualAck(p) = DemoFixtures.resumeNotManual.decoded() { return p }
        return ManualReply()
    }

    static var landResultTemplate: ManualReply {
        if case let .manualResult(p) = DemoFixtures.landResult.decoded() { return p }
        return ManualReply()
    }
}

/// A demo ground shared between the view model and the command path, safe to call from any
/// thread. The Ground tab advances it with its clock and feeds the events it returns into the
/// same handler a live ground's events go through.
public final class DemoGroundSession: @unchecked Sendable {
    private let lock = NSLock()
    private var ground: DemoGround

    public init(ground: DemoGround = DemoGround()) {
        self.ground = ground
    }

    public func advance(to elapsed: TimeInterval) -> [GroundEvent] {
        lock.withLock { ground.advance(to: elapsed) }
    }

    public func receive(_ command: GroundCommand, at elapsed: TimeInterval) {
        lock.withLock { ground.receive(command, at: elapsed) }
    }

    public var receivedCommands: [GroundCommand] { lock.withLock { ground.receivedCommands } }
    public var isFinished: Bool { lock.withLock { ground.isFinished } }
    public var now: TimeInterval { lock.withLock { ground.now } }
}

/// The rules for switching the demo on.
public enum DemoModePolicy {
    public enum StartStep: Equatable, Sendable {
        /// No live link: the demo starts at once.
        case startNow
        /// A live link is open or switched on: the user confirms that it is closed and switched
        /// off first. The demo never runs beside a live ground link.
        case confirmDisconnectFirst
        /// The demo cannot start now, for the reason given: live drones may be in the air, or a
        /// LAND ALL is unanswered or failed. Nothing changes; the live link stays as it is.
        case refused(String)
    }

    /// The state of the latest live LAND ALL.
    public enum LandAllState: Equatable, Sendable {
        /// No LAND ALL, or it succeeded.
        case settled
        /// Sent and not answered yet.
        case unanswered
        /// Failed, reached only some drones, or never answered. Stays until the user dismisses it.
        case failed
    }

    public static let refusedTitle = "The demo cannot start now"

    /// "2 drones are in the air. Land them first. ..." for drones the live ground reports flying
    /// (or hovering, held or manual). `stale` is true when that report is not live.
    public static func flyingRefusal(_ count: Int, stale: Bool) -> String {
        let noun = count == 1 ? "1 drone" : "\(count) drones"
        let verb = stale ? (count == 1 ? "was in the air at the last update from the ground" : "were in the air at the last update from the ground")
            : (count == 1 ? "is in the air" : "are in the air")
        let them = count == 1 ? "it" : "them"
        return "\(noun) \(verb). Land \(them) first. The demo closes the ground link, and LAND ALL on this phone would then reach only the simulated drones."
    }

    public static let landAllUnansweredRefusal =
        "A LAND ALL has no answer from the ground yet. Wait for its result before starting the demo."
    public static let landAllFailedRefusal =
        "The last LAND ALL failed, reached only some drones or got no answer. Make sure every drone has landed and dismiss that result before starting the demo."

    /// - Parameters:
    ///   - flyingDrones: the live drones that are, or may be, in the air.
    ///   - flyingStateIsStale: true when that count comes from values that are not live.
    ///   - landAll: the latest live LAND ALL.
    public static func startStep(
        liveLinkOpen: Bool, liveLinkSwitchedOn: Bool,
        flyingDrones: Int = 0, flyingStateIsStale: Bool = false, landAll: LandAllState = .settled
    ) -> StartStep {
        if flyingDrones > 0 { return .refused(flyingRefusal(flyingDrones, stale: flyingStateIsStale)) }
        switch landAll {
        case .unanswered: return .refused(landAllUnansweredRefusal)
        case .failed: return .refused(landAllFailedRefusal)
        case .settled: break
        }
        return liveLinkOpen || liveLinkSwitchedOn ? .confirmDisconnectFirst : .startNow
    }
}
