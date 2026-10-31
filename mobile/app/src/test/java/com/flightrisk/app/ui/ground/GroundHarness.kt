package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.ConnectionStatus
import com.flightrisk.app.ground.ContractFixtures
import com.flightrisk.app.ground.FakeClock
import com.flightrisk.app.ground.FakeConnection
import com.flightrisk.app.ground.FakePort
import com.flightrisk.app.ground.GroundConsoleClient
import com.flightrisk.app.ground.GroundClientConfig
import com.flightrisk.app.ground.HealthProbeResult
import com.flightrisk.app.ground.SecretAvailability
import com.flightrisk.app.ground.SecretName
import com.flightrisk.app.ground.SecretStore
import com.flightrisk.app.ground.SecretStoragePort
import com.flightrisk.app.ground.TEST_KEY
import com.flightrisk.app.ground.eventually
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import java.util.concurrent.Executors

/** A secret storage the tests can make unavailable, like a broken Keystore. */
class TestStorage(initial: Map<String, String> = emptyMap(), var broken: Boolean = false) : SecretStoragePort {
    val map = LinkedHashMap(initial)

    override fun read(key: String): String? = if (broken) null else map[key]

    override fun write(changes: Map<String, String?>): Boolean {
        if (broken) return false
        for ((k, v) in changes) if (v == null) map.remove(k) else map[k] = v
        return true
    }

    override fun availability(): SecretAvailability =
        if (broken) SecretAvailability.Unavailable("could not open the secure storage (KeyStoreException)") else SecretAvailability.Available
}

/**
 * A [GroundConsoleViewModel] over the real [GroundConsoleClient] with a fake
 * transport, a fake secure storage and in-memory settings. The "main thread"
 * is one dedicated thread, so the ViewModel and the client share the same
 * single-threaded confinement the app has.
 */
class GroundHarness(
    address: String? = "127.0.0.1:5555",
    key: String? = TEST_KEY,
    enabled: Boolean = true,
    broken: Boolean = false,
    var probeResult: HealthProbeResult = HealthProbeResult.Ok("1.0", com.flightrisk.app.ground.StatusSummary(true, "edge", 2)),
) {
    val port = FakePort()
    val clientClock = FakeClock()
    val vmClock = FakeClock()
    val storage = TestStorage(if (key != null) mapOf(SecretName.DASHBOARD_API_KEY.storageKey to key) else emptyMap(), broken)
    val secrets = SecretStore(storage, isMainThread = { Thread.currentThread().name == "fake-main" })
    val settings = MemoryGroundSettings(address, enabled)
    private val executor = Executors.newSingleThreadExecutor { r -> Thread(r, "fake-main").apply { isDaemon = true } }
    private val main = executor.asCoroutineDispatcher()
    private val cleanup = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    val probedWith = mutableListOf<String>()

    val vm = GroundConsoleViewModel(
        secrets = secrets,
        settings = settings,
        clientFactory = { endpoint, k, scope ->
            GroundConsoleClient(
                endpoint, k, scope, port = port, config = GroundClientConfig(), emitPolicy = GroundConsoleViewModel.EMIT_POLICY,
                clock = clientClock, random = { 0.0 },
            )
        },
        probe = { endpoint, _ ->
            probedWith += endpoint.hostHeader
            probeResult
        },
        clock = vmClock,
        mainDispatcher = main,
        ioDispatcher = Dispatchers.IO,
        startTicker = false,
        cleanupScope = cleanup,
    )

    val ui: GroundUiState get() = vm.uiState.value

    private val open = ContractFixtures.frame("engineio_open")
    private val ack = ContractFixtures.frame("socketio_connect_ack")
    val connectError = ContractFixtures.frame("socketio_connect_error")

    fun <T> onMain(block: () -> T): T = runBlocking(main) { block() }

    /** The app comes to the foreground. */
    suspend fun foreground() {
        vm.onForeground()
        vm.awaitIdle()
    }

    /** Foreground, then a complete connect with the server playing the fake ground. */
    suspend fun connect(index: Int = 0): FakeConnection {
        if (index == 0) foreground()
        val c = port.connection(index)
        c.server(open)
        eventually("namespace connect sent") { c.sent.isNotEmpty() }
        c.server(ack)
        eventually("link connected") { ui.link == GroundLink.Connected }
        return c
    }

    fun FakeConnection.fleetStatus(case: String = "all_connected") = server(ContractFixtures.eventFrame("fleet_status.json", case))
    fun FakeConnection.fixture(file: String, case: String) = server(ContractFixtures.eventFrame(file, case))
    fun FakeConnection.event(name: String, payloadJson: String) = server(ContractFixtures.rawEventFrame(name, payloadJson))

    /** A connect with a fresh fleet status already shown. */
    suspend fun connectWithFleet(case: String = "all_connected"): FakeConnection {
        val c = connect()
        c.fleetStatus(case)
        eventually("fleet shown") { ui.cards.isNotEmpty() && !ui.isStateStale }
        return c
    }

    /** Moves the ViewModel clock and runs its tick. */
    fun advance(ms: Long) {
        vmClock.advance(ms)
        onMain { vm.tick() }
    }

    /** Lets the client's backoff pass and returns once it is connecting again. */
    suspend fun advanceBackoff() {
        eventually("reconnecting") { clientStatus() is ConnectionStatus.Reconnecting }
        val delayMs = (clientStatus() as ConnectionStatus.Reconnecting).retryInMs
        val deadline = clientClock.nowMs() + delayMs
        eventually("backoff sleeper") { deadline in clientClock.sleepers() }
        clientClock.advance(delayMs)
    }

    private fun clientStatus(): ConnectionStatus? = onMain { vm.currentClientStatusForTest() }

    /** Server closes the session. */
    suspend fun drop(c: FakeConnection) {
        c.server("41")
        eventually("link lost") { ui.link != GroundLink.Connected }
    }

    fun close() {
        onMain { vm.release() }
        cleanup.cancel()
        executor.shutdownNow()
    }

    companion object {
        const val KEY = TEST_KEY
    }
}

fun harnessTest(
    address: String? = "127.0.0.1:5555",
    key: String? = TEST_KEY,
    enabled: Boolean = true,
    broken: Boolean = false,
    block: suspend GroundHarness.() -> Unit,
) = runBlocking {
    val h = GroundHarness(address, key, enabled, broken)
    try {
        h.block()
        // The key must never be in anything the screen can show.
        assertNoKey(h.ui.toString())
    } finally {
        h.close()
    }
}

fun assertNoKey(text: String) {
    if (text.contains(TEST_KEY)) throw AssertionError("the API key is in: ${text.take(300)}")
}
