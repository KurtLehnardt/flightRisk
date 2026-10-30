package com.flightrisk.contract

import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The edge-link rules for a phone edge against `edge_ground_to_edge.json` and `edge_edge_to_ground.json`. */
class EdgeLinkTest {
    private val inbound = Fixtures.cases("edge_ground_to_edge.json")

    @Test
    fun onlyStreamVideoChangesAnything() {
        for ((name, case) in inbound) {
            val action = EdgeLink.actionFor(case["payload"].toString())
            if (name == "stream_video") {
                assertEquals(EdgeAction.SetThumbnail(true), action)
            } else {
                assertEquals(name, EdgeAction.Ignore, action)
            }
        }
        assertEquals(EdgeAction.SetThumbnail(false), EdgeLink.actionFor("""{"type":"stream_video","enabled":false}"""))
        assertEquals(EdgeAction.Ignore, EdgeLink.actionFor("""{"type":"stream_video","enabled":"no"}"""))
    }

    @Test
    fun typeFilterIgnoresProtocolTrafficTargetsAndUnknownTypes() {
        for (type in listOf("hello", "heartbeat", "clock", "ping", "set_target", "land", "emergency_land_all", "warp_drive")) {
            assertEquals(type, EdgeAction.Ignore, EdgeLink.actionFor("""{"type":"$type"}"""))
        }
        assertEquals(EdgeLink.IGNORED_PROTOCOL_TYPES, setOf("hello", "heartbeat", "clock", "ping"))
        for (garbage in listOf("", "not json", "[1,2]", "{}", """{"type":7}""")) {
            assertEquals(garbage, EdgeAction.Ignore, EdgeLink.actionFor(garbage))
        }
    }

    @Test
    fun parsedTypes() {
        val target = EdgeLink.parse(inbound.getValue("set_target")["payload"]!!.jsonObject)
        assertEquals(GroundToEdgeMessage.SetTarget(listOf(0.12, -0.08, 0.33, 0.05), null), target)
        assertEquals(GroundToEdgeMessage.Ignored("hello"), EdgeLink.parse(inbound.getValue("ground_hello")["payload"]!!.jsonObject))
        assertEquals(GroundToEdgeMessage.Ignored("land"), EdgeLink.parse(inbound.getValue("land")["payload"]!!.jsonObject))
    }

    @Test
    fun phoneNeverSendsFleetEdgeReplies() {
        assertTrue((EdgeLink.OUTBOUND_TYPES intersect EdgeLink.NEVER_SENT_TYPES).isEmpty())
        val replies = Fixtures.cases("edge_edge_to_ground.json")
            .filterKeys { !it.startsWith("detections") }
            .mapValues { (_, c) -> EdgeReply.fromJson(c["payload"]!!.jsonObject) }
        for ((name, reply) in replies) assertTrue(name, reply.type in EdgeLink.NEVER_SENT_TYPES)
        assertEquals(ReasonCode("drone_unreachable"), replies.getValue("nack_v2_drone_unreachable").reason)
        assertEquals("set_detection_profile", replies.getValue("profile_nack_in_flight").forCommand)
        for (reply in replies.values) reply.reason?.let { assertTrue(it.raw, it.known) }
    }

    @Test
    fun authMessageAndCloseCode() {
        val auth = ContractJson.parseToJsonElement(EdgeLink.authMessage("t0k")).jsonObject
        assertEquals(setOf("type", "token"), auth.keys)
        assertEquals("auth", auth["type"].stringValue())
        assertEquals(4001, EdgeLink.CLOSE_UNAUTHORIZED)
    }
}
