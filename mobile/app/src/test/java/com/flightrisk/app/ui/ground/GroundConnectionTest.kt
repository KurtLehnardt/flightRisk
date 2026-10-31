package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.HealthProbeResult
import com.flightrisk.app.ground.SecretName
import com.flightrisk.app.ground.TEST_KEY
import com.flightrisk.app.ground.eventually
import kotlinx.coroutines.delay
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The connection card, secret handling and the foreground/background lifecycle. */
class GroundConnectionTest {

    private fun GroundHarness.keyStored() = storage.map[SecretName.DASHBOARD_API_KEY.storageKey]

    // ---- never started until configured and enabled

    @Test
    fun `nothing connects until an address and a key are saved and the link is switched on`() = harnessTest(address = null, key = null, enabled = false) {
        foreground()
        assertEquals(GroundLink.NotConfigured, ui.link)
        assertEquals(KeyState.NONE, ui.connection.keyState)
        onMain { vm.saveAddress("127.0.0.1") }
        vm.awaitIdle()
        assertEquals("127.0.0.1:5555", ui.connection.savedAddress)
        assertTrue(port.opened.isEmpty())
        onMain { vm.saveKey(TEST_KEY) }
        vm.awaitIdle()
        assertEquals(KeyState.SAVED, ui.connection.keyState)
        assertEquals(GroundLink.Off, ui.link)
        delay(100)
        assertTrue(port.opened.isEmpty())
        onMain { vm.setEnabled(true) }
        vm.awaitIdle()
        port.connection(0)
        eventually("connecting") { ui.link is GroundLink.Connecting }
    }

    @Test
    fun `a configured and enabled link connects on the foreground and not before`() = harnessTest {
        delay(100)
        assertTrue(port.opened.isEmpty())
        foreground()
        port.connection(0)
    }

    // ---- address validation

    @Test
    fun `an address is validated with the endpoint parser and the reason is shown`() = harnessTest(address = null, key = null, enabled = false) {
        onMain { vm.saveAddress("https://192.168.1.20:5555") }
        assertTrue(ui.connection.addressError!!.contains("https and wss are not supported"))
        assertNull(settings.address)
        onMain { vm.saveAddress("wss://host") }
        assertTrue(ui.connection.addressError!!.contains("https and wss are not supported"))
        onMain { vm.saveAddress("") }
        assertEquals("Enter the ground station's address.", ui.connection.addressError)
        onMain { vm.saveAddress("host/path") }
        assertTrue(ui.connection.addressError!!.startsWith("Enter only the host and port"))
        onMain { vm.saveAddress("host:99999") }
        assertEquals("The port must be a number from 1 to 65535.", ui.connection.addressError)
        onMain { vm.saveAddress("http://192.168.1.20:6000/") }
        vm.awaitIdle()
        assertNull(ui.connection.addressError)
        assertEquals("192.168.1.20:6000", settings.address)
    }

    // ---- the key

    @Test
    fun `the key is stored only in the secret store, never echoed and never in the settings`() = harnessTest(address = "127.0.0.1", key = null, enabled = false) {
        foreground()
        onMain { vm.saveKey("  $TEST_KEY  ") }
        vm.awaitIdle()
        assertEquals(TEST_KEY, keyStored())
        assertEquals(KeyState.SAVED, ui.connection.keyState)
        assertEquals("Key saved.", ui.connection.message)
        assertFalse(ui.toString().contains(TEST_KEY))
        assertFalse(settings.toString().contains(TEST_KEY))
        assertFalse(settings.address.orEmpty().contains(TEST_KEY))
    }

    @Test
    fun `a key with spaces or control characters is refused before it is stored`() = harnessTest(address = "127.0.0.1", key = null, enabled = false) {
        onMain { vm.saveKey("two words") }
        vm.awaitIdle()
        assertNull(keyStored())
        assertTrue(ui.connection.message!!.startsWith("Enter the dashboard API key"))
        onMain { vm.saveKey("   ") }
        vm.awaitIdle()
        assertNull(keyStored())
    }

    @Test
    fun `remove saved key deletes it and closes the link`() = harnessTest {
        val c = connectWithFleet()
        onMain { vm.removeKey() }
        vm.awaitIdle()
        assertNull(keyStored())
        assertEquals(KeyState.NONE, ui.connection.keyState)
        assertEquals(GroundLink.NotConfigured, ui.link)
        eventually("socket closed") { c.closed }
        // The last state stays visible, marked stale.
        assertTrue(ui.isStateStale)
        assertEquals(2, ui.cards.size)
    }

    @Test
    fun `a secure storage failure says so and fails safe`() = harnessTest(broken = true) {
        foreground()
        assertEquals(KeyState.UNAVAILABLE, ui.connection.keyState)
        assertEquals(GroundLink.StorageUnavailable, ui.link)
        assertTrue(ui.link.text.startsWith("Secure storage unavailable on this device"))
        delay(100)
        assertTrue(port.opened.isEmpty())
        onMain { vm.saveKey(TEST_KEY) }
        vm.awaitIdle()
        assertEquals("Secure storage unavailable on this device", ui.connection.message)
        assertEquals(KeyState.UNAVAILABLE, ui.connection.keyState)
        vm.landAll().join()
        assertEquals(GroundText.LAND_ALL_NOT_SENT_STORAGE, ui.landAllText)
        onMain { vm.testConnection() }
        vm.awaitIdle()
        assertEquals("Secure storage unavailable on this device", ui.connection.testResult)
        assertTrue(port.opened.isEmpty())
        assertTrue(probedWith.isEmpty())
    }

    @Test
    fun `secret storage is never touched on the main thread`() = harnessTest(address = "127.0.0.1", key = null, enabled = true) {
        // The harness secret store throws on the fake main thread; this would fail if the view model read or wrote there.
        foreground()
        onMain { vm.saveKey(TEST_KEY) }
        vm.awaitIdle()
        onMain { vm.testConnection() }
        vm.awaitIdle()
        onMain { vm.removeKey() }
        vm.awaitIdle()
        assertEquals(KeyState.NONE, ui.connection.keyState)
    }

    // ---- test connection

    @Test
    fun `test connection reports the probe result without the key`() = harnessTest {
        onMain { vm.testConnection() }
        vm.awaitIdle()
        assertEquals("Reachable, key accepted (ground version 1.0).", ui.connection.testResult)
        assertEquals(listOf("127.0.0.1:5555"), probedWith)
        probeResult = HealthProbeResult.WrongKey("1.0")
        onMain { vm.testConnection() }
        vm.awaitIdle()
        assertEquals("Reachable, but the ground refused the key (ground version 1.0).", ui.connection.testResult)
        probeResult = HealthProbeResult.Unreachable("ConnectException: refused")
        onMain { vm.testConnection() }
        vm.awaitIdle()
        assertTrue(ui.connection.testResult!!.startsWith("Unreachable: ConnectException: refused"))
        // Testing never starts the live link.
        assertTrue(port.opened.isEmpty())
    }

    @Test
    fun `test connection without a key or address says what is missing`() = harnessTest(address = null, key = null, enabled = false) {
        onMain { vm.testConnection() }
        vm.awaitIdle()
        assertEquals("Save the ground station address first.", ui.connection.testResult)
        onMain { vm.saveAddress("127.0.0.1") }
        onMain { vm.testConnection() }
        vm.awaitIdle()
        assertEquals("Save the dashboard API key first.", ui.connection.testResult)
        assertTrue(probedWith.isEmpty())
    }

    // ---- lifecycle

    @Test
    fun `going to the background shuts the client down and keeps the last state stale`() = harnessTest {
        val c = connectWithFleet()
        onMain { vm.onBackground() }
        assertEquals(GroundLink.Paused, ui.link)
        assertTrue(ui.isStateStale)
        assertEquals(2, ui.cards.size)
        assertEquals("LAST: CONNECTED", ui.cards.first().linkChipText)
        eventually("socket left the namespace and closed") { c.closed && c.sent.contains("41") }
    }

    @Test
    fun `returning rebuilds the client and the old values stay stale until a fresh fleet status`() = harnessTest {
        val c = connectWithFleet("one_held_orphan_countdown")
        onMain { vm.onBackground() }
        eventually("closed") { c.closed }
        vm.onForeground()
        vm.awaitIdle()
        val second = connect(1)
        assertEquals(2, port.opened.size)
        // The new client knows nothing yet; the screen still shows the last values, stale.
        assertEquals(2, ui.cards.size)
        assertTrue(ui.isStateStale)
        assertFalse(ui.cards.first { it.id == "drone-2" }.canResume)
        second.fleetStatus("all_connected")
        eventually("fresh") { !ui.isStateStale }
        assertEquals(null, ui.cards.first { it.id == "drone-2" }.controlChip)
    }

    @Test
    fun `background does not start a connection when the link is off`() = harnessTest(enabled = false) {
        onMain { vm.onBackground() }
        vm.onForeground()
        vm.awaitIdle()
        assertEquals(GroundLink.Off, ui.link)
        assertTrue(port.opened.isEmpty())
    }

    @Test
    fun `releasing the view model shuts the client down`() = harnessTest {
        val c = connectWithFleet()
        onMain { vm.release() }
        eventually("socket closed") { c.closed }
        assertTrue(c.sent.contains("41"))
    }

    @Test
    fun `switching the link off closes the client and switching it on reconnects`() = harnessTest {
        val c = connectWithFleet()
        onMain { vm.setEnabled(false) }
        eventually("closed") { c.closed }
        assertEquals(GroundLink.Off, ui.link)
        onMain { vm.setEnabled(true) }
        vm.awaitIdle()
        port.connection(1)
    }

    @Test
    fun `a different ground address forgets the old grounds state`() = harnessTest {
        val c = connectWithFleet()
        vm.landDrone("drone-1").join()
        onMain { vm.saveAddress("127.0.0.2:5556") }
        vm.awaitIdle()
        eventually("old socket closed") { c.closed }
        assertTrue(ui.cards.isEmpty())
        assertTrue(ui.banners.isEmpty())
        assertEquals("127.0.0.2:5556", ui.connection.savedAddress)
        port.connection(1)
    }

    @Test
    fun `the privacy line is on the screen`() {
        assertEquals("Connects only to the ground station address you enter.", GroundText.PRIVACY_NOTE)
    }
}
