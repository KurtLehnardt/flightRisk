import Foundation

/// A decoded server event. Events this build does not know are `.unknown` and must be ignored.
public enum GroundEvent: Equatable, Sendable {
    case status(StatusPayload)
    case fleetStatus(FleetStatusPayload)
    case fleetEvent(FleetEvent)
    case fleetPlan(FleetPlanPayload)
    case fleetDetection(FleetDetection)
    case frame(FramePayload)
    case matchAlert(MatchAlert)
    case matchAlertReminder(MatchAlert)
    case detectionProfiles(DetectionProfilesPayload)
    case ceilingSettings(CeilingSettingsPayload)
    case reasoningProvider(ReasoningProviderPayload)
    case manualStatus(ManualStatusPayload)
    case manualAck(ManualAck)
    case manualResult(ManualResult)
    case landAllAck(LandAllAck)
    case commandAck(CommandAck)
    case streamVideoState(StreamVideoState)
    case error(ErrorPayload)
    case warning(ErrorPayload)
    /// An event name this build does not handle.
    case unknown(name: String)
    /// A known event whose payload was not a JSON object.
    case undecodable(name: String)

    /// Every event name this build decodes.
    public static let handledEventNames: [String] = [
        "status", "fleet_status", "fleet_event", "fleet_plan", "fleet_detection", "frame",
        "match_alert", "match_alert_reminder", "detection_profiles", "ceiling_settings",
        "reasoning_provider", "manual_status", "manual_ack", "manual_result", "land_all_ack",
        "command_ack", "stream_video_state", "error", "warning",
    ]

    /// Decode the payload (the one JSON object of a `42["name",payload]` frame).
    public static func decode(name: String, payload: Data, decoder: JSONDecoder = JSONDecoder()) -> GroundEvent {
        func make<T: Decodable>(_ type: T.Type, _ wrap: (T) -> GroundEvent) -> GroundEvent {
            guard let v = try? decoder.decode(type, from: payload) else { return .undecodable(name: name) }
            return wrap(v)
        }
        switch name {
        case "status": return make(StatusPayload.self, GroundEvent.status)
        case "fleet_status": return make(FleetStatusPayload.self, GroundEvent.fleetStatus)
        case "fleet_event": return make(FleetEvent.self, GroundEvent.fleetEvent)
        case "fleet_plan": return make(FleetPlanPayload.self, GroundEvent.fleetPlan)
        case "fleet_detection": return make(FleetDetection.self, GroundEvent.fleetDetection)
        case "frame": return make(FramePayload.self, GroundEvent.frame)
        case "match_alert": return make(MatchAlert.self, GroundEvent.matchAlert)
        case "match_alert_reminder": return make(MatchAlert.self, GroundEvent.matchAlertReminder)
        case "detection_profiles": return make(DetectionProfilesPayload.self, GroundEvent.detectionProfiles)
        case "ceiling_settings": return make(CeilingSettingsPayload.self, GroundEvent.ceilingSettings)
        case "reasoning_provider": return make(ReasoningProviderPayload.self, GroundEvent.reasoningProvider)
        case "manual_status": return make(ManualStatusPayload.self, GroundEvent.manualStatus)
        case "manual_ack": return make(ManualAck.self, GroundEvent.manualAck)
        case "manual_result": return make(ManualResult.self, GroundEvent.manualResult)
        case "land_all_ack": return make(LandAllAck.self, GroundEvent.landAllAck)
        case "command_ack": return make(CommandAck.self, GroundEvent.commandAck)
        case "stream_video_state": return make(StreamVideoState.self, GroundEvent.streamVideoState)
        case "error": return make(ErrorPayload.self, GroundEvent.error)
        case "warning": return make(ErrorPayload.self, GroundEvent.warning)
        default: return .unknown(name: name)
        }
    }
}
