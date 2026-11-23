import Foundation

// The contract fixture payloads the demo ground replays (`tests/fixtures/mobile_contract/`),
// copied here because the app cannot read the repository at run time. Each is the fixture case's
// payload as compact JSON with sorted keys, minus the image keys named in `droppedKeys`: the demo
// carries no images at all. `DemoFixtureTests` compares every template with the committed fixture
// and fails when one drifts; copy the case again when that happens.

enum DemoFixtures {
    struct Template: Sendable {
        let file: String
        let caseName: String
        let event: String
        /// Image keys left out of the fixture payload.
        let droppedKeys: [String]
        let json: String

        /// The payload decoded exactly as a payload from a live ground is.
        func decoded() -> GroundEvent {
            GroundEvent.decode(name: event, payload: Data(json.utf8))
        }
    }

    static let status = Template(
        file: "status", caseName: "fleet_two_drones", event: "status", droppedKeys: [],
        json: #"{"appearance_only":true,"connected":true,"fleet":{"drones":2,"net":"station","synced":false},"source":"edge","tiled":false}"#
    )

    static let fleetStatus = Template(
        file: "fleet_status", caseName: "all_connected", event: "fleet_status", droppedKeys: [],
        json: #"{"drones":{"drone-1":{"acked":false,"alt_cm":100,"battery":87,"connected":true,"control_mode":"autonomous","drone_id":"drone-1","drone_link_detail":null,"drone_link_ok":true,"edge_control_mode":"autonomous","heading_deg":0.0,"height_cm":100,"is_flying":false,"lane_index":null,"lanes_total":null,"link_age_s":0.1,"lost":false,"manual_last":null,"manual_supported":true,"offset_ms":250.0,"orphan_land_in_s":null,"rel_x_cm":0.0,"rel_y_cm":0.0,"rtt_ms":0.0,"skew_ms":null,"synced":true},"drone-2":{"acked":false,"alt_cm":130,"battery":87,"connected":true,"control_mode":"autonomous","drone_id":"drone-2","drone_link_detail":null,"drone_link_ok":true,"edge_control_mode":"autonomous","heading_deg":0.0,"height_cm":100,"is_flying":false,"lane_index":null,"lanes_total":null,"link_age_s":0.1,"lost":false,"manual_last":null,"manual_supported":true,"offset_ms":250.0,"orphan_land_in_s":null,"rel_x_cm":250.0,"rel_y_cm":0.0,"rtt_ms":0.0,"skew_ms":null,"synced":true}},"manual_controlled":null,"net":"station","synced":true}"#
    )

    static let fleetPlan = Template(
        file: "fleet_plan", caseName: "two_drones", event: "fleet_plan", droppedKeys: [],
        json: #"{"gap_cm":100,"grid":{"h_cm":400,"w_cm":400,"x_cm":0,"y_cm":0},"strip_cm":75,"uncovered":[],"zones":[{"alt_cm":100,"drone_id":"drone-1","h_cm":400,"lanes":[{"direction":"forward","distance_cm":400},{"direction":"right","distance_cm":75},{"direction":"back","distance_cm":400}],"lost":false,"w_cm":150,"x_cm":0,"y_cm":0},{"alt_cm":130,"drone_id":"drone-2","h_cm":400,"lanes":[{"direction":"forward","distance_cm":400},{"direction":"right","distance_cm":75},{"direction":"back","distance_cm":400}],"lost":false,"w_cm":150,"x_cm":250,"y_cm":0}]}"#
    )

    static let profiles = Template(
        file: "detection_profiles", caseName: "unsupported", event: "detection_profiles", droppedKeys: [],
        json: #"{"controls_enabled":true,"controls_note":null,"drones":{"drone-1":{"active":"default","desired":"default","detector_ok":null,"drone_id":"drone-1","fallback":false,"fallback_reason":null,"flying":false,"mismatch":false,"note":null,"override":null,"pending":false,"refusal":null,"requested":null,"supported":true,"switchable":true},"drone-2":{"active":null,"desired":"default","detector_ok":null,"drone_id":"drone-2","fallback":false,"fallback_reason":null,"flying":false,"mismatch":false,"note":null,"override":null,"pending":false,"refusal":null,"requested":null,"supported":false,"switchable":null}},"fleet_default":"default","profiles":[{"confidence":0.4,"description":"The edge detector as it was before profiles: yolo11n at confidence 0.4, tiled only when FLIGHTRISK_DETECTOR_TILED is on.","face_weight_scale":1.0,"fallback":null,"label":"Startup default (yolo11n)","min_person_px":null,"name":"default","reid_weight_scale":1.0,"tiled":null,"view":"any","weights":"yolo11n.pt","weights_required":false},{"confidence":0.5,"description":"People seen from above: the HERIDAL fine-tuned YOLO11s on overlapping tiles.","face_weight_scale":1.0,"fallback":"level","label":"Top-down search (HERIDAL)","min_person_px":32,"name":"top_down","reid_weight_scale":1.0,"tiled":true,"view":"nadir","weights":"models/heridal_yolo11s.pt","weights_required":true},{"confidence":0.4,"description":"More direct views of people and faces: the general yolo11n on the whole frame.","face_weight_scale":1.0,"fallback":null,"label":"Level flight (faces)","min_person_px":null,"name":"level","reid_weight_scale":1.0,"tiled":false,"view":"oblique","weights":"yolo11n.pt","weights_required":false}]}"#
    )

    static let profilesRefused = Template(
        file: "detection_profiles", caseName: "refused", event: "detection_profiles", droppedKeys: [],
        json: #"{"controls_enabled":true,"controls_note":null,"drones":{"drone-1":{"active":"default","desired":"top_down","detector_ok":null,"drone_id":"drone-1","fallback":false,"fallback_reason":null,"flying":true,"mismatch":true,"note":null,"override":"top_down","pending":false,"refusal":{"detail":"detection profiles change only on the ground","reason":"in_flight"},"requested":"top_down","supported":true,"switchable":false}},"fleet_default":null,"profiles":[{"confidence":0.4,"description":"The edge detector as it was before profiles: yolo11n at confidence 0.4, tiled only when FLIGHTRISK_DETECTOR_TILED is on.","face_weight_scale":1.0,"fallback":null,"label":"Startup default (yolo11n)","min_person_px":null,"name":"default","reid_weight_scale":1.0,"tiled":null,"view":"any","weights":"yolo11n.pt","weights_required":false},{"confidence":0.5,"description":"People seen from above: the HERIDAL fine-tuned YOLO11s on overlapping tiles.","face_weight_scale":1.0,"fallback":"level","label":"Top-down search (HERIDAL)","min_person_px":32,"name":"top_down","reid_weight_scale":1.0,"tiled":true,"view":"nadir","weights":"models/heridal_yolo11s.pt","weights_required":true},{"confidence":0.4,"description":"More direct views of people and faces: the general yolo11n on the whole frame.","face_weight_scale":1.0,"fallback":null,"label":"Level flight (faces)","min_person_px":null,"name":"level","reid_weight_scale":1.0,"tiled":false,"view":"oblique","weights":"yolo11n.pt","weights_required":false}]}"#
    )

    static let ceilingOnGround = Template(
        file: "ceiling_settings", caseName: "on_ground_in_sync", event: "ceiling_settings", droppedKeys: [],
        json: #"{"ceiling_cm":200,"drones":[{"default_slot_cm":100,"drone_id":"drone-1","edge":{"ceiling_cm":200.0,"flying":false,"in_sync":true,"margin_cm":60.0,"note":null,"pending":false,"refusal":null,"slot_cm":100.0,"supported":true},"own_slot_cm":null,"problem":null,"slot_cm":100},{"default_slot_cm":130,"drone_id":"drone-2","edge":{"ceiling_cm":200.0,"flying":false,"in_sync":true,"margin_cm":60.0,"note":null,"pending":false,"refusal":null,"slot_cm":130.0,"supported":true},"own_slot_cm":null,"problem":null,"slot_cm":130}],"editable":true,"file":"/home/operator/.flightrisk/ceiling.json","limits":{"max_ceiling_cm":1000,"max_slot_cm":500,"min_ceiling_cm":150,"min_margin_cm":30,"min_slot_cm":20},"load_error":null,"locked_reason":null,"margin_cm":60,"max_slot_cm":140}"#
    )

    static let ceilingInFlight = Template(
        file: "ceiling_settings", caseName: "locked_in_flight", event: "ceiling_settings", droppedKeys: [],
        json: #"{"ceiling_cm":200,"drones":[{"default_slot_cm":100,"drone_id":"drone-1","edge":{"ceiling_cm":200.0,"flying":true,"in_sync":true,"margin_cm":60.0,"note":null,"pending":false,"refusal":null,"slot_cm":100.0,"supported":true},"own_slot_cm":null,"problem":null,"slot_cm":100},{"default_slot_cm":130,"drone_id":"drone-2","edge":{"ceiling_cm":200.0,"flying":true,"in_sync":true,"margin_cm":60.0,"note":null,"pending":false,"refusal":null,"slot_cm":130.0,"supported":true},"own_slot_cm":null,"problem":null,"slot_cm":130}],"editable":false,"file":"/home/operator/.flightrisk/ceiling.json","limits":{"max_ceiling_cm":1000,"max_slot_cm":500,"min_ceiling_cm":150,"min_margin_cm":30,"min_slot_cm":20},"load_error":null,"locked_reason":"the ceiling and the altitude slots change only while every drone is on the ground and no start is scheduled (drone-1 reported flying)","margin_cm":60,"max_slot_cm":140}"#
    )

    static let reasoningProvider = Template(
        file: "reasoning_provider", caseName: "local_gemma_cloud_blocked", event: "reasoning_provider", droppedKeys: [],
        json: #"{"available":["gemma","off"],"cloud_allowed":false,"label":"Gemma","model":"gemma4:latest","provider":"gemma","unavailable":{"claude":"Cloud reasoning is not enabled. Claude would send person crops and the reference photo to Anthropic; set FLIGHTRISK_ALLOW_CLOUD_LLM=1 to opt in."}}"#
    )

    static let streamVideoOff = Template(
        file: "stream_video_state", caseName: "off", event: "stream_video_state", droppedKeys: [],
        json: #"{"enabled":false}"#
    )

    static let manualStatus = Template(
        file: "manual_status", caseName: "held_orphan_countdown", event: "manual_status", droppedKeys: [],
        json: #"{"controlled":null,"drones":{"drone-1":{"control_mode":"autonomous","edge_control_mode":"autonomous","manual_last":null,"manual_supported":true,"orphan_land_in_s":null},"drone-2":{"control_mode":"held","edge_control_mode":"held","manual_last":null,"manual_supported":true,"orphan_land_in_s":120}}}"#
    )

    static let start = Template(
        file: "fleet_event", caseName: "start", event: "fleet_event", droppedKeys: [],
        json: #"{"detail":"start_at in 2 s","event":"start"}"#
    )

    static let landAllEvent = Template(
        file: "fleet_event", caseName: "land_all", event: "fleet_event", droppedKeys: [],
        json: #"{"detail":"sent to all","event":"land_all"}"#
    )

    static let frameDetectionsOnly = Template(
        file: "frame", caseName: "edge_detections_only", event: "frame", droppedKeys: [],
        json: #"{"boxes":[{"bbox_norm":[0.10416666666666667,0.16666666666666666,0.15625,0.3194444444444444],"matched":false,"score":0.31,"track_id":null}],"drone_id":"drone-2","fps":0.0,"frame_height":720,"frame_width":960,"image":null,"match":false,"match_score":0.31,"persons":1,"recording":false,"stream_video":false,"telemetry":{}}"#
    )

    static let frameWithMatch = Template(
        file: "frame", caseName: "edge_video_with_match", event: "frame", droppedKeys: ["image"],
        json: #"{"boxes":[{"bbox_norm":[0.4166666666666667,0.4166666666666667,0.4791666666666667,0.5833333333333334],"matched":true,"score":0.86,"track_id":null},{"bbox_norm":[0.10416666666666667,0.16666666666666666,0.15625,0.3194444444444444],"matched":false,"score":0.31,"track_id":null}],"drone_id":"drone-1","fps":0.0,"frame_height":720,"frame_width":960,"match":true,"match_score":0.86,"persons":2,"recording":false,"stream_video":true,"telemetry":{}}"#
    )

    static let detection = Template(
        file: "fleet_detection", caseName: "new_detection", event: "fleet_detection", droppedKeys: ["thumbnail"],
        json: #"{"count":1,"drone_id":"drone-1","drone_ids":["drone-1"],"id":1,"rel_x_cm":120.0,"rel_y_cm":80.0,"x_cm":120.0,"y_cm":80.0}"#
    )

    static let alert = Template(
        file: "match_alert", caseName: "edge_match", event: "match_alert", droppedKeys: ["snapshot"],
        json: #"{"alert_id":"00000000000040008000000000000001","alert_level":"possible_match","face_score":0.0,"gemma_confidence":null,"gemma_match":null,"reasoning":null,"reid_score":0.86,"score":0.86,"time":"12:34:56","track_id":"8_7","type":"edge"}"#
    )

    static let alertConfirmed = Template(
        file: "match_alert", caseName: "resolved_confirm", event: "match_alert_resolved", droppedKeys: [],
        json: #"{"action":"confirm","alert_id":"00000000000040008000000000000002","ok":true,"state":{"alert_id":"00000000000040008000000000000002","phase":"resolved","renotify_count":0,"resolution":"confirmed"}}"#
    )

    static let landAllAck = Template(
        file: "land_all_ack", caseName: "fleet_reached_all", event: "land_all_ack", droppedKeys: [],
        json: #"{"coordinator":2,"local":null,"reached_any":true,"transport":2}"#
    )

    static let landAck = Template(
        file: "manual_ack", caseName: "land_ok", event: "manual_ack", droppedKeys: [],
        json: #"{"action":"land","detail":null,"drone_id":"drone-1","ok":true,"reason":null}"#
    )

    static let landUnknownDrone = Template(
        file: "manual_ack", caseName: "land_unknown_drone", event: "manual_ack", droppedKeys: [],
        json: #"{"action":"land","detail":"'drone-9' is not in the fleet","drone_id":"drone-9","ok":false,"reason":"invalid"}"#
    )

    static let resumeNotManual = Template(
        file: "manual_ack", caseName: "resume_search_not_manual", event: "manual_ack", droppedKeys: [],
        json: #"{"action":"resume_search","detail":"drone-1 is not under manual control","drone_id":"drone-1","ok":false,"reason":"not_manual"}"#
    )

    static let landResult = Template(
        file: "manual_result", caseName: "land_acked", event: "manual_result", droppedKeys: [],
        json: #"{"action":"land","detail":null,"drone_id":"drone-1","ok":true,"reason":null}"#
    )

    static let all: [Template] = [
        status, fleetStatus, fleetPlan, profiles, profilesRefused, ceilingOnGround, ceilingInFlight, reasoningProvider, streamVideoOff, manualStatus, start, landAllEvent, frameDetectionsOnly, frameWithMatch, detection, alert, alertConfirmed, landAllAck, landAck, landUnknownDrone, resumeNotManual, landResult,
    ]
}
