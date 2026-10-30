package com.flightrisk.contract

import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.longOrNull
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Engine.IO and Socket.IO text frames against `socketio_frames.json`. */
class SocketIoFramesTest {
    private val cases = Fixtures.cases("socketio_frames.json")

    @Test
    fun everyFrameDecodesToItsTypesAckIdAndData() {
        for ((name, case) in cases) {
            val frame = case["frame"].stringValue()!!
            val parsed = SocketIoFrames.parse(frame) ?: bad("$name did not parse")
            assertEquals(name, case["engineio_type"].stringValue(), parsed.type.name)
            val sioType = case["socketio_type"]
            val decoded = case["decoded"]
            if (sioType == null || sioType is JsonNull) {
                assertNull(name, parsed.socketIo)
                if (decoded != null) assertTrue(name, jsonSemanticallyEqual(decoded, parsed.data))
            } else {
                val packet = parsed.socketIo ?: bad("$name has no Socket.IO packet")
                assertEquals(name, sioType.stringValue(), packet.type.name)
                assertEquals(name, (case["ack_id"] as? JsonPrimitive)?.longOrNull, packet.ackId)
                assertEquals(name, "/", packet.namespace)
                if (decoded != null) assertTrue(name, jsonSemanticallyEqual(decoded, packet.data))
            }
        }
    }

    @Test
    fun clientFramesAreEncodedByteForByte() {
        assertEquals(cases.getValue("socketio_connect_with_auth")["frame"].stringValue(), SocketIoFrames.connect("test-key"))
        assertEquals(cases.getValue("socketio_connect_without_auth")["frame"].stringValue(), SocketIoFrames.connect(null))
        assertEquals(
            cases.getValue("socketio_event_client_to_server")["frame"].stringValue(),
            SocketIoFrames.event(ClientCommand.GetFleetStatus),
        )
        assertEquals(
            cases.getValue("socketio_event_client_to_server_with_payload")["frame"].stringValue(),
            SocketIoFrames.event(ClientCommand.landDrone("drone-1")!!),
        )
        assertEquals(
            cases.getValue("socketio_event_with_ack_id")["frame"].stringValue(),
            SocketIoFrames.event(ClientCommand.GetFleetStatus, ackId = 1),
        )
        assertEquals(cases.getValue("engineio_pong")["frame"].stringValue(), SocketIoFrames.PONG)
        assertEquals(cases.getValue("socketio_disconnect")["frame"].stringValue(), SocketIoFrames.DISCONNECT)
        assertEquals(cases.getValue("engineio_close")["frame"].stringValue(), SocketIoFrames.CLOSE)
    }

    @Test
    fun serverEventFrameDecodesToItsModel() {
        val packet = SocketIoFrames.parse(cases.getValue("socketio_event_server_to_client")["frame"].stringValue()!!)!!.socketIo!!
        assertEquals("land_all_ack", packet.eventName)
        assertEquals(LandAllAck(2, 2, null, true), ServerEvents.decode(packet.eventName!!, packet.eventPayload))
    }

    @Test
    fun connectFramesNeverPrintTheKey() {
        val parsed = SocketIoFrames.parse(SocketIoFrames.connect("test-key"))!!
        assertFalse(parsed.toString().contains("test-key"))
        assertFalse(parsed.socketIo.toString().contains("test-key"))
    }

    @Test
    fun invalidFramesReturnNull() {
        for (bad in listOf("", "9", "4", "49", "42[\"x\"", "0{bad")) assertNull(bad, SocketIoFrames.parse(bad))
        assertEquals("/admin", SocketIoFrames.parse("40/admin,")!!.socketIo!!.namespace)
    }
}
