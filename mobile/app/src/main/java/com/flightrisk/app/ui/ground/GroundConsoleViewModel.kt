package com.flightrisk.app.ui.ground

import android.content.Context
import com.flightrisk.app.demo.DemoGroundPort
import com.flightrisk.app.demo.DemoScenario
import com.flightrisk.app.demo.DemoText
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import com.flightrisk.app.ground.AndroidGroundLog
import com.flightrisk.app.ground.ConnectionStatus
import com.flightrisk.app.ground.DashboardApiKey
import com.flightrisk.app.ground.DashboardRestClient
import com.flightrisk.app.ground.EmitPolicy
import com.flightrisk.app.ground.EndpointParseResult
import com.flightrisk.app.ground.FailureReason
import com.flightrisk.app.ground.GroundClock
import com.flightrisk.app.ground.GroundConsoleClient
import com.flightrisk.app.ground.GroundEndpoint
import com.flightrisk.app.ground.GroundEvent
import com.flightrisk.app.ground.GroundLog
import com.flightrisk.app.ground.SocketIoPort
import com.flightrisk.app.ground.GroundSnapshot
import com.flightrisk.app.ground.HealthProbeResult
import com.flightrisk.app.ground.NotSentReason
import com.flightrisk.app.ground.SecretAvailability
import com.flightrisk.app.ground.SecretName
import com.flightrisk.app.config.AppSecrets
import com.flightrisk.app.ground.SecretStore
import com.flightrisk.app.ground.SendResult
import com.flightrisk.contract.AlertUpgrade
import com.flightrisk.contract.ClientCommand
import com.flightrisk.contract.CommandAck
import com.flightrisk.contract.ConnectionState
import com.flightrisk.contract.ControlModeDisplay
import com.flightrisk.contract.DetectionProfiles
import com.flightrisk.contract.DisplayText
import com.flightrisk.contract.DroneBoard
import com.flightrisk.contract.DroneRecord
import com.flightrisk.contract.FleetEvent
import com.flightrisk.contract.FleetPlan
import com.flightrisk.contract.FleetStatus
import com.flightrisk.contract.Frame
import com.flightrisk.contract.LandAllAck
import com.flightrisk.contract.ManualAction
import com.flightrisk.contract.MatchAlert
import com.flightrisk.contract.MatchAlertResolved
import com.flightrisk.contract.ManualDroneStatus
import com.flightrisk.contract.ManualReply
import com.flightrisk.contract.ManualStatus
import com.flightrisk.contract.ReasoningResult
import com.flightrisk.contract.ServerEvent
import com.flightrisk.contract.ServerMessage
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The Ground console: one connection to one FlightRisk ground station
 * (docs/mobile-contract.md), the drones as the ground reports them, and the
 * three commands this screen may send that move a drone: LAND ALL, land one
 * drone and resume a held drone's search.
 *
 * Safety rules this class keeps (docs/mobile-contract.md, "Safety display rules"):
 * - LAND ALL and per-drone Land are never disabled by any drone, fleet or
 *   connection state. When nothing could be sent, the screen says so plainly;
 *   nothing is ever queued and nothing is resent automatically.
 * - "Sent" only means "handed to the connection". The ground's answer is the
 *   proof, a missing answer becomes a visible "no reply" or "no final answer"
 *   state, and a link lost while a command waits becomes a visible failure.
 * - Any refused or failed manual reply, whatever its action (an orphan
 *   auto-land included), stays on the drone's card until dismissed.
 * - Drone state comes only from the client's `board`, which feeds every
 *   `fleet_status` and `manual_status` through the contract's reducer in
 *   arrival order. This class never merges the two itself. State from before
 *   a disconnect is kept but marked stale until a fresh `fleet_status`.
 * - The only way to the wire is the client's command functions, with an emit
 *   policy narrowed to what this screen needs ([EMIT_POLICY]).
 * - The API key goes from [SecretStore] straight into [DashboardApiKey] and
 *   from there only into the client. It is never kept here, shown or logged.
 *
 * Threading: everything runs on [mainDispatcher] (Android's main thread in
 * the app); the client is main-thread safe, and the secure storage is read
 * and written only through [SecretStore.load] and [SecretStore.save].
 *
 * Demo mode ([enterDemo], [exitDemo]) replaces the ground with [DemoGroundPort], which plays a recorded
 * mission inside the app. While it is on, no real client is created ([clientFactory] is never called),
 * no health probe runs ([probe] is never called), and every command is answered by the demo port on the
 * phone. It never starts while the real ground's state needs the operator: a real drone reads as flying,
 * a LAND ALL or a Land waits for its answer or failed, or a failure banner or drone alert shows
 * ([DemoRequest.Blocked]); this is checked again right before it starts. When any real ground state
 * exists (a client, the link switched on, or anything a ground reported), it starts only after the user
 * confirmed ([DemoRequest.NeedsDisconnect]); it then switches the real link off, saves that, and closes the
 * client. Leaving the demo leaves the link off and says so. Turning it on and off clears every drone,
 * alert, frame and command result, so demo data never mixes with a real ground's, and every demo result
 * the screen shows starts with "DEMO:".
 */
class GroundConsoleViewModel(
    private val secrets: SecretStore,
    private val settings: GroundSettingsStore,
    private val clientFactory: (GroundEndpoint, DashboardApiKey, CoroutineScope) -> GroundConsoleClient = ::defaultClient,
    private val probe: suspend (GroundEndpoint, DashboardApiKey) -> HealthProbeResult =
        { endpoint, key -> DashboardRestClient(endpoint, key).probe() },
    private val clock: GroundClock = GroundClock.SYSTEM,
    private val mainDispatcher: CoroutineDispatcher = Dispatchers.Main.immediate,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
    private val startTicker: Boolean = true,
    private val clientDispatcher: CoroutineDispatcher = Dispatchers.Default,
    private val cleanupScope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
    /** Loads the demo mission, or null when this build has none. Called off the main thread's work, once per demo start. */
    private val demoScenario: (suspend () -> DemoScenario?)? = null,
    /** The demo ground's in-process transport. Tests wrap it; it never touches the network. */
    private val demoPortFactory: (DemoScenario, CoroutineScope) -> SocketIoPort = { s, cs -> DemoGroundPort(s, cs) },
    /** The clock of the demo client and its replay. */
    private val demoClock: GroundClock = GroundClock.SYSTEM,
) : ViewModel() {

    private val scope = CoroutineScope(SupervisorJob() + mainDispatcher)
    private var tickerJob: Job? = null

    private val _ui = MutableStateFlow(GroundUiState())

    /** Everything the Ground screen draws. */
    val uiState: StateFlow<GroundUiState> = _ui.asStateFlow()

    // Connection
    private var client: GroundConsoleClient? = null
    private var clientScope: CoroutineScope? = null
    private var collectorScope: CoroutineScope? = null
    private var link: GroundLink = GroundLink.NotConfigured
    private var keyState = KeyState.UNKNOWN
    private var inBackground = true

    // Demo mode. While [demoOn] is true, nothing may start a real client.
    private var demo = false
    private var demoProblem: String? = null

    /** A dialog the app must show about demo mode (a refusal or a confirmation), until [dismissDemoNotice]. */
    private var demoNotice: DemoNotice? = null

    /** "Demo ended. The ground link is off…", shown after the demo until the link is turned on again. */
    private var demoEndedNote: String? = null

    /** The real ground's address when the demo started, for [demoEndedNote]. */
    private var demoLinkHost: String? = null

    /** Bumped by every demo start and stop, so a mission that finishes loading late starts nothing. */
    private var demoToken = 0
    private val demoOn: Boolean get() = demo

    /** Bumped by everything that closes or replaces the client, so a slow key read cannot start a stale one. */
    private var epoch = 0

    /** Bumped each time a connection is lost; a command sent before it cannot be answered. */
    private var lossEpoch = 0

    // Drone state bookkeeping
    private var fleetFresh = false
    private var freshInThisClient = false
    private var retainedBoard: DroneBoard? = null
    private var retainedSnapshot: GroundSnapshot? = null
    private var staleSinceMs: Long? = null
    private val orphanBase = HashMap<String, Pair<Double, Long>>()
    private var lastFleetStatusAt = 0L
    private var noRecentUpdate = false

    // Video tiles and match alerts (read only; the phone never starts or stops video and never answers an alert)
    private var receivedFrames: Map<String, ReceivedFrame> = emptyMap()
    private val alertsLog = GroundAlertsLog()

    // Command feedback
    private var attemptCounter = 0
    private var idCounter = 0L
    private var landAllFeedback: CommandFeedback? = null
    private val landFeedback = LinkedHashMap<String, CommandFeedback>()
    private val resumeFeedback = LinkedHashMap<String, CommandFeedback>()
    private val droneAlerts = LinkedHashMap<String, CommandFeedback>()
    private var banners: List<GroundBanner> = emptyList()
    private var toasts: List<GroundToast> = emptyList()

    /** LAND ALL taps handed to the connection and not yet answered, oldest first. */
    private val landAllOutstanding = ArrayList<Pair<Int, Long>>()

    /** Drones whose land was sent to a ground without a fleet, oldest first; each `command_ack` answers the oldest. */
    private val pendingLegacyLands = ArrayList<String>()

    // Settings. Read from the preferences file off the main thread, then kept here.
    private var addressSetting: String? = null
    private var enabledSetting = false
    private var settingsLoaded = false
    private var settingsJob: Job? = null
    private var addressError: String? = null
    private var message: String? = null
    private var testing = false
    private var testResult: String? = null

    init {
        keyState = KeyState.UNKNOWN
        link = idleLink()
        startTicker()
        settingsJob = scope.launch {
            val (address, enabled) = withContext(ioDispatcher) { settings.address to settings.enabled }
            if (!settingsLoaded) {
                addressSetting = address
                enabledSetting = enabled
                settingsLoaded = true
            }
            refreshKeyState()
        }
        publish()
    }

    private fun startTicker() {
        if (!startTicker || tickerJob?.isActive == true) return
        tickerJob = scope.launch {
            while (true) {
                delay(TICK_MS)
                tick()
            }
        }
    }

    // ---------------------------------------------------------------- lifecycle

    /** The app came to the foreground: connect when the link is configured and switched on. */
    fun onForeground() {
        inBackground = false
        startTicker()
        scope.launch {
            settingsJob?.join()
            refreshKeyState()
            connectIfWanted()
        }
    }

    /**
     * The app went to the background: close the link (the client leaves the
     * namespace and stops heartbeating) and mark the state stale. The phone
     * cannot act on the drones from the background anyway.
     */
    fun onBackground() {
        inBackground = true
        // Nothing on the screen can change in the background, so the one second ticker stops.
        tickerJob?.cancel()
        tickerJob = null
        if (demoOn) {
            // The demo runs in memory only; it keeps its place while the app is away.
            publish()
            return
        }
        epoch++
        if (client != null) {
            closeClient()
            link = GroundLink.Paused
        } else if (link == GroundLink.Stopped) {
            link = idleLink()
        }
        publish()
    }

    /** Releases the client (it leaves the namespace) and stops every timer. Called when the ViewModel clears. */
    fun release() {
        closeClient()
        scope.cancel()
    }

    override fun onCleared() = release()

    // ---------------------------------------------------------------- settings actions

    /** Validates and saves the ground address. Reconnects to the new address when the link is on. */
    fun saveAddress(text: String) {
        when (val parsed = GroundEndpoint.parseDetailed(text)) {
            is EndpointParseResult.Invalid -> {
                addressError = parsed.reason
                publish()
            }
            is EndpointParseResult.Ok -> {
                addressError = null
                val normalized = parsed.endpoint.savedText
                val changed = normalized != addressSetting
                settingsLoaded = true
                addressSetting = normalized
                scope.launch { withContext(ioDispatcher) { settings.address = normalized } }
                if (changed && !demoOn) forgetGroundState()
                if (changed) {
                    // A different ground: the demo's "turn it on to reconnect to <host>" no longer applies.
                    demoEndedNote = null
                    demoLinkHost = null
                }
                message = "Address saved."
                restart()
            }
        }
    }

    /**
     * Saves the dashboard API key in the secure storage. The caller clears
     * its text field at once; the key is never shown again.
     */
    fun saveKey(text: String) {
        val trimmed = text.trim()
        if (DashboardApiKey.of(trimmed) == null) {
            message = "Enter the dashboard API key (the ground's FLIGHTRISK_API_KEY): printable characters, no spaces."
            publish()
            return
        }
        scope.launch {
            val saved = secrets.save(SecretName.DASHBOARD_API_KEY, trimmed)
            if (!saved) {
                refreshKeyState()
                message = if (keyState == KeyState.UNAVAILABLE) {
                    GroundText.SECURE_STORAGE_UNAVAILABLE
                } else {
                    "The key could not be saved."
                }
                publish()
                return@launch
            }
            keyState = KeyState.SAVED
            message = "Key saved."
            restart()
        }
    }

    /** Deletes the saved key and closes the link. */
    fun removeKey() {
        scope.launch {
            val removed = secrets.save(SecretName.DASHBOARD_API_KEY, null)
            if (!removed) {
                refreshKeyState()
                message = if (keyState == KeyState.UNAVAILABLE) GroundText.SECURE_STORAGE_UNAVAILABLE else "The key could not be removed."
                publish()
                return@launch
            }
            keyState = KeyState.NONE
            message = "Key removed."
            if (!demoOn) {
                epoch++
                closeClient()
                link = idleLink()
            }
            publish()
        }
    }

    /** Switches the link on or off. On connects only when an address and a key are saved. */
    fun setEnabled(enabled: Boolean) {
        if (enabled && demoOn) {
            // Demo mode switched the real link off; it stays off until the demo ends.
            message = DemoText.LINK_ON_IN_DEMO
            publish()
            return
        }
        settingsLoaded = true
        enabledSetting = enabled
        scope.launch { withContext(ioDispatcher) { settings.enabled = enabled } }
        if (enabled) demoEndedNote = null
        if (!demoOn) {
            epoch++
            if (!enabled && client != null) closeClient()
        }
        link = if (client == null) idleLink() else link
        publish()
        if (enabled) scope.launch { connectIfWanted() }
    }

    /** "Test connection": the REST health probe with the saved address and key. The result never contains the key. */
    fun testConnection() {
        if (testing) return
        if (demoOn) {
            // The probe would reach the real ground; demo mode sends nothing off the phone.
            testResult = DemoText.TEST_IN_DEMO
            publish()
            return
        }
        scope.launch {
            testing = true
            testResult = null
            publish()
            testResult = runProbe()
            testing = false
            publish()
        }
    }

    /** The banner's "Retry": ends a wrong-key or backoff wait at once. */
    fun retryConnection() {
        val c = client
        if (c != null) {
            c.retryNow()
        } else {
            scope.launch {
                refreshKeyState()
                connectIfWanted()
            }
        }
    }

    // ---------------------------------------------------------------- demo mode

    /** What turning demo mode on would do now. */
    sealed interface DemoRequest {
        /** No real ground state exists: demo mode can start at once. */
        data object Ready : DemoRequest

        /**
         * Real ground state exists (a client, the link switched on, or anything a ground reported); starting the
         * demo switches the link off and clears it, so ask the user first. [host] is the ground's address, if known.
         */
        data class NeedsDisconnect(val host: String?) : DemoRequest

        /** The real ground's state needs the operator; demo mode must not hide it. [reason] says what to do. */
        data class Blocked(val reason: String) : DemoRequest

        data object AlreadyOn : DemoRequest

        /** This build has no demo mission. */
        data object Unavailable : DemoRequest
    }

    /** A dialog about demo mode that the app shows until [dismissDemoNotice]. */
    sealed interface DemoNotice {
        /** Ask before the demo switches the real link off. */
        data class ConfirmDisconnect(val host: String?) : DemoNotice

        /** Say why demo mode did not start. */
        data class Blocked(val text: String) : DemoNotice
    }

    /** True while demo mode is on. */
    val isDemo: Boolean get() = demoOn

    /** Guards against a second start while the mission loads. */
    private var demoLoading = false

    fun demoRequest(): DemoRequest = when {
        demoOn -> DemoRequest.AlreadyOn
        demoScenario == null -> DemoRequest.Unavailable
        else -> realSafetyBlock()?.let { DemoRequest.Blocked(it) }
            ?: if (hasRealGroundState()) DemoRequest.NeedsDisconnect(realHost()) else DemoRequest.Ready
    }

    /**
     * Why the real ground's state must stay on screen, or null when demo mode may replace it: a real drone
     * reads as flying (live or kept from before a disconnect), a LAND ALL or a Land waits for its answer, the
     * last LAND ALL failed or reached only some drones, a failure banner, a drone alert or a failed Land shows,
     * or the real link is up or switched on but the ground has not reported its drones yet.
     */
    private fun realSafetyBlock(): String? {
        val board = displayBoard()
        val flying = board.drones.values.filter { readsAsFlying(it, board) }.map { it.id }
        if (flying.isNotEmpty()) return DemoText.blockedByFlying(flying)
        val all = landAllFeedback
        if (landAllOutstanding.isNotEmpty() || all?.isWaiting == true) return DemoText.BLOCKED_BY_LAND_ALL_WAITING
        if (all != null && (all.phase == CommandPhase.FAILED || all.phase == CommandPhase.PARTIAL)) {
            return DemoText.BLOCKED_BY_LAND_ALL_FAILED
        }
        if (pendingLegacyLands.isNotEmpty() || landFeedback.values.any { it.isWaiting }) return DemoText.BLOCKED_BY_LAND_WAITING
        if (banners.any { it.isFailure } || droneAlerts.isNotEmpty() || landFeedback.values.any { it.isFailure }) {
            return DemoText.BLOCKED_BY_FAILURE
        }
        // The real link is up or wanted, but the ground has not said which drones fly: the phone cannot tell
        // whether demo mode would hide a drone in the air (the iOS app refuses here too).
        if ((client != null || enabledSetting) && lastFleetStatusAt == 0L && board.drones.isEmpty()) {
            return DemoText.BLOCKED_BY_FLEET_UNKNOWN
        }
        return null
    }

    /** A drone the ground reports in the air: flying, held (hovering) or under manual control. */
    private fun readsAsFlying(record: DroneRecord, board: DroneBoard): Boolean {
        val mode = board.controlMode(record.id)
        if (mode is ControlModeDisplay.Held || mode == ControlModeDisplay.Manual) return true
        return when (record) {
            is DroneRecord.Fleet -> record.drone.isFlying == true
            is DroneRecord.Legacy -> record.drone.isFlying == true
        }
    }

    /** True when anything of a real ground exists that demo mode would switch off or clear. */
    private fun hasRealGroundState(): Boolean =
        client != null || (enabledSetting && addressSetting != null) || displayBoard().drones.isNotEmpty() ||
            alertsLog.size > 0 || landAllFeedback != null || banners.isNotEmpty() || toasts.isNotEmpty() ||
            landFeedback.isNotEmpty() || resumeFeedback.isNotEmpty() || droneAlerts.isNotEmpty() ||
            receivedFrames.isNotEmpty() || retainedSnapshot != null || profileFeedback != null

    /** The real ground's address as `host:port`, or null when none is known. */
    private fun realHost(): String? =
        client?.endpoint?.hostHeader
            ?: (addressSetting?.let { GroundEndpoint.parseDetailed(it) } as? EndpointParseResult.Ok)?.endpoint?.hostHeader
            ?: addressSetting

    /**
     * Turns demo mode on. Nothing happens, and [GroundUiState.demoNotice] says why, while the real ground's
     * state needs the operator ([DemoRequest.Blocked]) or while real ground state exists and [disconnectConfirmed]
     * is false (the user has not agreed in a dialog yet). Both are checked again right before the demo starts,
     * after the mission has loaded. Starting switches the real link off (and saves that, so a restart during the
     * demo does not reconnect), closes the real client and clears everything the screen showed.
     * [isDemo] and [GroundUiState.demo] tell whether it started; [GroundUiState.demoProblem] says why not when
     * the mission could not be loaded.
     */
    fun enterDemo(disconnectConfirmed: Boolean = false): Job = scope.launch { startDemo(disconnectConfirmed) }

    /**
     * The demo dialog was answered (or closed). The notice stays in this ViewModel until then, so the
     * dialog survives a rotation or any other recreation of the activity.
     */
    fun dismissDemoNotice() {
        demoNotice = null
        publish()
    }

    /** Shows a refusal the app decided itself (a search runs, or a Tello drone is connected on the Search tab). */
    fun showDemoRefusal(text: String) {
        demoNotice = DemoNotice.Blocked(text)
        publish()
    }

    /** Says why the demo did not start, or asks for the confirmation; true when it may start now. */
    private fun mayStartDemo(disconnectConfirmed: Boolean): Boolean {
        val blocked = realSafetyBlock()
        if (blocked != null) {
            demoNotice = DemoNotice.Blocked(blocked)
            publish()
            return false
        }
        if (hasRealGroundState() && !disconnectConfirmed) {
            // Also when a real client connected after the app last asked: never fail silently.
            demoNotice = DemoNotice.ConfirmDisconnect(realHost())
            publish()
            return false
        }
        return true
    }

    private suspend fun startDemo(disconnectConfirmed: Boolean): Boolean {
        if (demoOn || demoLoading) return demo
        settingsJob?.join()
        val loader = demoScenario
        if (loader == null) {
            demoProblem = DemoText.UNAVAILABLE
            demoNotice = DemoNotice.Blocked(DemoText.UNAVAILABLE)
            publish()
            return false
        }
        if (!mayStartDemo(disconnectConfirmed)) return false
        val token = ++demoToken
        demoLoading = true
        val scenario = try {
            loader()
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (_: Exception) {
            null
        } finally {
            demoLoading = false
        }
        if (token != demoToken || demoOn) return false // Left (or restarted) while the mission loaded.
        if (scenario == null) {
            demoProblem = DemoText.UNAVAILABLE
            demoNotice = DemoNotice.Blocked(DemoText.UNAVAILABLE)
            publish()
            return false
        }
        // The real ground may have changed while the mission loaded: check again, right before anything is cleared.
        if (!mayStartDemo(disconnectConfirmed)) return false
        demoNotice = null
        demoProblem = null
        demoEndedNote = null
        demoLinkHost = realHost()
        // Switch the real link off and save that first, so no real event can arrive once the demo shows and a
        // restart while the demo runs does not reconnect.
        settingsLoaded = true
        enabledSetting = false
        scope.launch { withContext(ioDispatcher) { settings.enabled = false } }
        epoch++
        clearAllGroundState()
        demo = true
        attachClient { cs ->
            GroundConsoleClient(
                DemoGroundPort.ENDPOINT, null, cs,
                port = demoPortFactory(scenario, cs),
                emitPolicy = EMIT_POLICY,
                clock = demoClock,
                restClientFactory = { _, _ -> throw IllegalStateException("demo mode makes no REST requests") },
                logSink = GroundLog.NONE,
            )
        }
        return true
    }

    /**
     * Turns demo mode off: the demo ground is closed and every drone, alert, frame, banner and command
     * result it produced is cleared. The real ground link stays off (demo mode switched it off); the
     * Ground screen says so and how to reconnect.
     */
    fun exitDemo() {
        if (demoLoading) {
            // Turned off while the mission loads: the start that is under way finds a new token and starts nothing.
            demoToken++
            publish()
            return
        }
        if (!demoOn) return
        demoToken++
        demo = false
        demoProblem = null
        demoNotice = null
        epoch++
        clearAllGroundState()
        // Name the ground only when turning the link on would reconnect to it (an address and a key are saved).
        demoEndedNote = if (addressSetting != null || demoLinkHost != null) {
            DemoText.ended(if (keyState == KeyState.SAVED) demoLinkHost ?: realHost() else null)
        } else {
            null
        }
        demoLinkHost = null
        link = idleLink()
        publish()
    }

    /** Closes the client and forgets everything any ground (real or demo) reported. */
    private fun clearAllGroundState() {
        forgetGroundState()
        if (client != null) closeClient()
        retainedBoard = null
        retainedSnapshot = null
        landAllFeedback = null
        toasts = emptyList()
        banners = emptyList()
        testResult = null
        fleetFresh = false
        freshInThisClient = false
        noRecentUpdate = false
        lastFleetStatusAt = 0L
    }

    // ---------------------------------------------------------------- commands

    /**
     * LAND ALL. Never disabled, never confirmed, never queued. Always leaves a visible result. Whether demo
     * mode was on is recorded at the tap: if it changed before the command goes out, nothing is sent and the
     * screen says so, so a tap on the real LAND ALL can never be answered by the demo, or the other way round.
     */
    fun landAll(): Job {
        val tappedInDemo = demoOn
        return scope.launch { sendLandAll(tappedInDemo) }
    }

    /** Lands one drone (`drone_command {command: "land"}`), in every drone state. */
    fun landDrone(id: String): Job = scope.launch { sendDroneCommand(id, isLand = true) }

    /** Resumes the search of a held drone nobody controls (`manual_resume_search`). Sent only when [canResume] is true. */
    fun resumeHeldDrone(id: String): Job = scope.launch {
        if (ClientCommand.resumeHeldDrone(id) == null) {
            refuseUnusableId(id, isLand = false)
        } else if (canResume(id)) {
            sendDroneCommand(id, isLand = false)
        } else {
            // The card offered Resume a moment ago; say why nothing was sent instead of doing nothing.
            val at = clock.nowMs()
            val text = "${GroundText.shortId(id)}: Resume search was not sent: ${resumeBlockedReason(id)}."
            setDroneFeedback(false, id, CommandFeedback(nextAttempt(), CommandPhase.FAILED, text, startedAtMs = at, updatedAtMs = at))
            publish()
        }
    }

    private fun resumeBlockedReason(id: String): String {
        val board = displayBoard()
        return when {
            !link.isConnected -> "this phone is not connected to the ground"
            isStale(board) -> "the drone state is not live"
            board.connection(id).isStale -> "the drone's own link is down"
            else -> "the ground no longer reports this drone as held"
        }
    }

    fun dismissLandAll() {
        landAllFeedback = null
        publish()
    }

    fun dismissBanner(id: Long) {
        banners = banners.filterNot { it.id == id }
        publish()
    }

    fun dismissToast(id: Long) {
        toasts = toasts.filterNot { it.id == id }
        publish()
    }

    fun dismissDroneAlert(id: String) {
        droneAlerts.remove(id)
        publish()
    }

    fun dismissDroneFeedback(id: String, isLand: Boolean) {
        (if (isLand) landFeedback else resumeFeedback).remove(id)
        publish()
    }

    /**
     * Whether to show "Resume search": the reducer says the drone is held
     * and nobody controls it, the drone's own link is up (the ground refuses
     * a resume for a DOWN or LOST drone), the state is live, and the link is up.
     */
    fun canResume(id: String): Boolean {
        val board = displayBoard()
        if (!board.canResumeSearch(id)) return false
        if (isStale(board) || !link.isConnected) return false
        return !board.connection(id).isStale
    }

    private suspend fun sendLandAll(tappedInDemo: Boolean) {
        val attempt = nextAttempt()
        val started = clock.nowMs()
        if (tappedInDemo != demoOn) {
            val text = if (tappedInDemo) DemoText.LAND_ALL_DEMO_ENDED else GroundText.LAND_ALL_NOT_SENT_DEMO_STARTED
            landAllFeedback = CommandFeedback(attempt, CommandPhase.FAILED, text, startedAtMs = started, updatedAtMs = started)
            publish()
            return
        }
        landAllFeedback = CommandFeedback(attempt, CommandPhase.SENDING, DisplayText.LAND_ALL_SENDING, startedAtMs = started, updatedAtMs = started)
        publish()
        val epochAtSend = lossEpoch
        val outcome = emit { it.landAll() }
        val at = clock.nowMs()
        val sentOnLiveLink = outcome == Outcome.SENT && lossEpoch == epochAtSend
        if (sentOnLiveLink) landAllOutstanding.add(attempt to at)
        if (landAllFeedback?.attempt != attempt) {
            publish()
            return // A newer tap took over.
        }
        when (outcome) {
            Outcome.SENT ->
                if (!sentOnLiveLink) {
                    landAllFeedback = failure(GroundText.LAND_ALL_CONNECTION_LOST, attempt, started)
                } else if (landAllFeedback?.phase == CommandPhase.SENDING) {
                    landAllFeedback = landAllFeedback?.copy(phase = CommandPhase.HANDED_OVER, text = GroundText.LAND_ALL_HANDED_OVER, updatedAtMs = at)
                }
            Outcome.NOT_CONNECTED -> {
                val text = if (link == GroundLink.WrongKey) DisplayText.LAND_ALL_NOT_SENT_WRONG_KEY else DisplayText.LAND_ALL_NOT_SENT
                landAllFeedback = failure(text, attempt, started)
            }
            Outcome.NO_CLIENT -> {
                val text = when {
                    link == GroundLink.StorageUnavailable -> GroundText.LAND_ALL_NOT_SENT_STORAGE
                    // Configured and on, but the client is not up yet (a cold start or a return to the foreground).
                    linkIsWanted() -> DisplayText.LAND_ALL_NOT_SENT
                    else -> GroundText.LAND_ALL_NOT_SENT_LINK_OFF
                }
                landAllFeedback = failure(text, attempt, started)
                if (linkIsWanted()) startConnecting()
            }
            Outcome.SEND_FAILED -> landAllFeedback = failure(GroundText.LAND_ALL_SEND_FAILED, attempt, started)
        }
        publish()
    }

    private suspend fun sendDroneCommand(id: String, isLand: Boolean) {
        val action = if (isLand) GroundText.ACTION_LAND else GroundText.ACTION_RESUME
        val command: ClientCommand = (if (isLand) ClientCommand.landDrone(id) else ClientCommand.resumeHeldDrone(id))
            ?: return refuseUnusableId(id, isLand)
        val attempt = nextAttempt()
        val started = clock.nowMs()
        setDroneFeedback(isLand, id, CommandFeedback(attempt, CommandPhase.SENDING, GroundText.droneSending(id, action), startedAtMs = started, updatedAtMs = started))
        publish()
        val epochAtSend = lossEpoch
        val outcome = emit { it.send(command) }
        val at = clock.nowMs()
        val sentOnLiveLink = outcome == Outcome.SENT && lossEpoch == epochAtSend
        if (sentOnLiveLink && isLand && board().drones[id] is DroneRecord.Legacy) pendingLegacyLands.add(id)
        if (droneFeedback(isLand, id)?.attempt != attempt) {
            publish()
            return
        }
        when (outcome) {
            Outcome.SENT ->
                if (!sentOnLiveLink) {
                    setDroneFeedback(isLand, id, failure(GroundText.droneConnectionLost(id, action), attempt, started))
                } else if (droneFeedback(isLand, id)?.phase == CommandPhase.SENDING) {
                    setDroneFeedback(isLand, id, CommandFeedback(attempt, CommandPhase.HANDED_OVER, GroundText.droneHandedOver(id, action), startedAtMs = started, updatedAtMs = at))
                }
            Outcome.NOT_CONNECTED -> setDroneFeedback(isLand, id, failure(GroundText.droneNotSent(id, action, linkOff = false), attempt, started))
            Outcome.NO_CLIENT -> {
                setDroneFeedback(isLand, id, failure(GroundText.droneNotSent(id, action, linkOff = !linkIsWanted()), attempt, started))
                if (linkIsWanted()) startConnecting()
            }
            Outcome.SEND_FAILED -> setDroneFeedback(isLand, id, failure(GroundText.droneSendFailed(id, action), attempt, started))
        }
        publish()
    }

    private fun refuseUnusableId(id: String, isLand: Boolean) {
        val action = if (isLand) GroundText.ACTION_LAND else GroundText.ACTION_RESUME
        val at = clock.nowMs()
        setDroneFeedback(isLand, id, CommandFeedback(nextAttempt(), CommandPhase.FAILED, GroundText.droneNoUsableId(action), startedAtMs = at, updatedAtMs = at))
        publish()
    }

    /** True when an address and a key are saved (or still being read) and the link is switched on. */
    private fun linkIsWanted(): Boolean {
        if (keyState == KeyState.NONE || keyState == KeyState.UNAVAILABLE) return false
        if (!settingsLoaded) return true
        return addressSetting != null && enabledSetting
    }

    private fun startConnecting() {
        if (inBackground) return
        scope.launch {
            settingsJob?.join()
            refreshKeyState()
            connectIfWanted()
        }
    }

    private enum class Outcome { SENT, NOT_CONNECTED, NO_CLIENT, SEND_FAILED }

    /** The one path to the wire: a client command function. Nothing is queued or retried. */
    private suspend fun emit(send: suspend (GroundConsoleClient) -> SendResult): Outcome {
        val c = client ?: return Outcome.NO_CLIENT
        return when (val result = send(c)) {
            is SendResult.Sent -> Outcome.SENT
            is SendResult.NotSent -> when (result.reason) {
                is NotSentReason.NotConnected -> Outcome.NOT_CONNECTED
                NotSentReason.NotStarted, NotSentReason.ShutDown -> Outcome.NO_CLIENT
                is NotSentReason.SendFailed, NotSentReason.BlockedByPolicy, NotSentReason.InvalidDroneId -> Outcome.SEND_FAILED
            }
        }
    }

    private fun nextAttempt(): Int = ++attemptCounter

    private fun failure(text: String, attempt: Int, startedAt: Long): CommandFeedback =
        CommandFeedback(attempt, CommandPhase.FAILED, text, startedAtMs = startedAt, updatedAtMs = clock.nowMs())

    private fun droneFeedback(isLand: Boolean, id: String): CommandFeedback? =
        if (isLand) landFeedback[id] else resumeFeedback[id]

    private fun setDroneFeedback(isLand: Boolean, id: String, value: CommandFeedback) {
        if (isLand) landFeedback[id] = value else resumeFeedback[id] = value
    }

    // ======== BEGIN mission cards (U4a/U4b): profile selection and plan currency ========
    // The profiles card is the only place that sends set_detection_profile and
    // set_fleet_detection_profile. Both go through the client's command functions and the
    // emit policy. Nothing is queued or resent. The ground is the authority: the phone only
    // refuses what the ground has already said cannot work (not live, controls off, unknown
    // profile or drone) and says why.

    /** False until a `fleet_plan` arrives in the current session; a plan kept from before a reconnect may no longer be the ground's plan. */
    private var planIsCurrent = false

    /** The result of the last profile selection. Cleared when the ground's next `detection_profiles` arrives. */
    private var profileFeedback: ProfileFeedback? = null

    /** Selects [profile] for [droneId]; an empty [profile] follows the fleet default. */
    fun selectDroneProfile(droneId: String, profile: String): Job = scope.launch { sendProfile(droneId, profile) }

    /** Selects [profile] as the fleet default; an empty [profile] clears the default. */
    fun selectFleetProfile(profile: String): Job = scope.launch { sendProfile(null, profile) }

    fun dismissProfileFeedback() {
        profileFeedback = null
        publish()
    }

    private fun currentSnapshot(): GroundSnapshot = client?.snapshot?.value ?: retainedSnapshot ?: GroundSnapshot()

    private fun refuseProfile(target: String?, why: String) {
        profileFeedback = ProfileFeedback(nextAttempt(), MissionFormat.profileNotSent(target, why), isFailure = true, awaitsAnswer = false)
        publish()
    }

    private suspend fun sendProfile(droneId: String?, profile: String) {
        val profiles = currentSnapshot().detectionProfiles
        if (profiles == null || !profiles.available) return refuseProfile(droneId, MissionFormat.WHY_NO_BOARD)
        MissionFormat.profileBlockedReason(profiles, isStale(displayBoard()), link.isConnected)?.let { return refuseProfile(droneId, it) }
        if (profile.isNotEmpty() && profiles.profiles.none { it.name == profile }) return refuseProfile(droneId, MissionFormat.WHY_UNKNOWN_PROFILE)
        if (droneId != null) {
            val state = profiles.drones[droneId] ?: return refuseProfile(droneId, MissionFormat.WHY_UNKNOWN_DRONE)
            if (state.supported != true) return refuseProfile(droneId, MissionFormat.WHY_UNSUPPORTED)
            // The dashboard disables the selector while a switch is pending.
            if (state.pending == true) return refuseProfile(droneId, MissionFormat.WHY_PENDING)
            // A selector never fires for an unchanged value, so an unchanged choice sends nothing.
            if ((state.override ?: "") == profile) return
            // A null factory result means "do not send".
            if (ClientCommand.setDetectionProfile(droneId, profile) == null) return refuseProfile(droneId, MissionFormat.WHY_NO_USABLE_ID)
        } else if ((profiles.fleetDefault ?: "") == profile) {
            return
        }
        val attempt = nextAttempt()
        val label = if (profile.isEmpty()) {
            if (droneId == null) MissionFormat.NO_FLEET_DEFAULT else MissionFormat.FOLLOW_FLEET_DEFAULT
        } else {
            MissionFormat.profileLabel(profiles, profile)
        }
        // Waiting is set before sending, as LAND ALL does: a fast reply must clear it, not be overwritten by it.
        profileFeedback = ProfileFeedback(
            attempt, MissionFormat.profileSent(droneId, label), isFailure = false, awaitsAnswer = true, startedAtMs = clock.nowMs(),
        )
        publish()
        val outcome = emit { if (droneId == null) it.setFleetDetectionProfile(profile) else it.setDetectionProfile(droneId, profile) }
        // Sent: if the link dropped afterwards, `onProfileLinkLost` already said the answer is unknown ("Check the
        // active profile"). The change may have reached the ground, so it is never reported as "NOT changed".
        val why = when {
            outcome == Outcome.SENT -> null
            outcome == Outcome.SEND_FAILED -> MissionFormat.WHY_SEND_FAILED
            else -> MissionFormat.WHY_NOT_CONNECTED
        }
        if (why != null) {
            profileFeedback = ProfileFeedback(attempt, MissionFormat.profileNotSent(droneId, why), isFailure = true, awaitsAnswer = false)
        }
        publish()
    }

    /** The ground answered with its profile state: the status lines now carry the result or refusal. */
    private fun onProfilesFromGround() {
        if (profileFeedback?.awaitsAnswer == true) profileFeedback = null
    }

    /** The link is gone while a selection waits: say so, because no answer can come. */
    private fun onProfileLinkLost() {
        val f = profileFeedback ?: return
        if (f.awaitsAnswer) {
            profileFeedback = f.copy(text = "Profile change: " + MissionFormat.WHY_LINK_LOST, isFailure = true, awaitsAnswer = false)
        }
    }
    // ======== END mission cards ========

    // ---------------------------------------------------------------- ticking

    /**
     * Moves the clock-driven state: unanswered commands become "no reply yet"
     * after 5 s, accepted commands without a final answer become failures
     * after 5 s + 4 s, success texts and warning toasts fade. Failures and
     * partial results never fade. Called once a second.
     */
    fun tick() {
        val now = clock.nowMs()
        landAllFeedback?.let { f ->
            if ((f.phase == CommandPhase.SENDING || f.phase == CommandPhase.HANDED_OVER) && now - f.startedAtMs >= REPLY_TIMEOUT_MS) {
                landAllFeedback = f.copy(phase = CommandPhase.NO_REPLY_YET, text = GroundText.LAND_ALL_NO_REPLY_YET, updatedAtMs = now)
            } else if (f.phase == CommandPhase.SUCCEEDED && now - f.updatedAtMs >= SUCCESS_FADE_MS && !demoOn) {
                // In demo mode the demo's answers stay, so a reviewer always sees what the demo did.
                landAllFeedback = null
            }
        }
        expireLandAll(now)
        profileFeedback?.let { f ->
            if (f.awaitsAnswer && now - f.startedAtMs >= REPLY_TIMEOUT_MS) {
                profileFeedback = f.copy(text = MissionFormat.PROFILE_NO_ANSWER, isFailure = true, awaitsAnswer = false)
            }
        }
        age(landFeedback, GroundText.ACTION_LAND, now)
        age(resumeFeedback, GroundText.ACTION_RESUME, now)
        checkFreshness(now)
        if (!demoOn && toasts.any { !it.isError && now - it.receivedAtMs >= WARNING_FADE_MS }) {
            toasts = toasts.filterNot { !it.isError && now - it.receivedAtMs >= WARNING_FADE_MS }
        }
        publish()
    }

    /**
     * The ground answers every LAND ALL within its own 35 s deadline. A tap
     * still unanswered after that (plus a margin) will never be answered, so it
     * stops counting: otherwise every later ack would be credited to the tap
     * before it.
     */
    private fun expireLandAll(now: Long) {
        val expired = landAllOutstanding.filter { now - it.second >= LAND_ALL_EXPIRES_MS }
        if (expired.isEmpty()) return
        landAllOutstanding.removeAll(expired)
        for ((attempt, _) in expired) {
            val current = landAllFeedback
            if (current != null && current.attempt == attempt && current.isWaiting) {
                landAllFeedback = current.copy(phase = CommandPhase.FAILED, text = GroundText.LAND_ALL_NO_ANSWER, updatedAtMs = now)
            } else {
                addBanner(GroundText.EARLIER_LAND_ALL_NO_ANSWER, true, now)
            }
        }
    }

    /** The ground keeps answering pings but stopped sending fleet_status: the values are no longer live. */
    private fun checkFreshness(now: Long) {
        if (link.isConnected && fleetFresh && now - lastFleetStatusAt >= FRESHNESS_TIMEOUT_MS) {
            fleetFresh = false
            noRecentUpdate = true
        }
    }

    private fun age(feedback: MutableMap<String, CommandFeedback>, action: String, now: Long) {
        for ((id, f) in feedback.entries.toList()) {
            val sinceStart = now - f.startedAtMs
            val sinceUpdate = now - f.updatedAtMs
            when {
                (f.phase == CommandPhase.SENDING || f.phase == CommandPhase.HANDED_OVER) && sinceStart >= REPLY_TIMEOUT_MS ->
                    feedback[id] = f.copy(phase = CommandPhase.NO_REPLY_YET, text = GroundText.droneNoReplyYet(id, action), updatedAtMs = now)
                f.phase == CommandPhase.ACCEPTED && f.awaitsFinalAnswer && sinceUpdate >= REPLY_TIMEOUT_MS + MANUAL_REPLY_TIMEOUT_MS ->
                    feedback[id] = f.copy(phase = CommandPhase.FAILED, text = GroundText.droneNoFinalAnswer(id, action), updatedAtMs = now)
                f.phase == CommandPhase.SUCCEEDED && sinceUpdate >= DRONE_SUCCESS_FADE_MS && !demoOn -> feedback.remove(id)
            }
        }
    }

    // ---------------------------------------------------------------- events

    /** Applies one client event. Every server event goes through here in arrival order. */
    internal fun handle(event: GroundEvent) {
        when (event) {
            is GroundEvent.SessionStarted -> {
                // The client re-requests the full state itself; the state stays stale until a fresh fleet_status.
                fleetFresh = false
                noRecentUpdate = false
                planIsCurrent = false // The ground sends its plan again after the state requests.
                alertsLog.onSessionStarted()
            }
            is GroundEvent.SessionEnded -> {
                fleetFresh = false
                planIsCurrent = false // Also covers events dropped by a stalled consumer.
                connectionLost()
            }
            is GroundEvent.Server -> handleServer(event.event)
        }
        publish()
    }

    private fun markFleetFresh(at: Long) {
        fleetFresh = true
        noRecentUpdate = false
        lastFleetStatusAt = at
        freshInThisClient = true
        retainedBoard = null
    }

    private fun handleServer(event: ServerEvent) {
        val at = clock.nowMs()
        when (event) {
            is FleetStatus.Fleet -> {
                orphanBase.clear()
                for ((id, d) in event.drones) d.orphanLandInS?.takeIf { it.isFinite() }?.let { orphanBase[id] = it to at }
                markFleetFresh(at)
            }
            is FleetStatus.Legacy -> {
                orphanBase.clear()
                markFleetFresh(at)
            }
            is ManualStatus -> for ((id, m) in event.drones) {
                if (ManualDroneStatus.KEY_ORPHAN_LAND_IN_S in m.carriedKeys) {
                    val v = m.orphanLandInS?.takeIf { it.isFinite() }
                    if (v != null) orphanBase[id] = v to at else orphanBase.remove(id)
                }
            }
            is FleetEvent -> {
                DisplayText.fleetBanner(event)?.let { addBanner(GroundText.shorten(it), DisplayText.isFailure(event), at) }
                DisplayText.refusal(event)?.let { addBanner(GroundText.shorten(it), true, at) }
                if (event is FleetEvent.LandAll && event.failed) {
                    // A failed LAND ALL stays in the top bar until dismissed or a new tap.
                    val current = landAllFeedback
                    landAllFeedback = CommandFeedback(
                        current?.attempt ?: nextAttempt(), CommandPhase.FAILED, GroundText.shorten(DisplayText.landAllFailed(GroundText.shorten(event.detail))),
                        startedAtMs = current?.startedAtMs ?: at, updatedAtMs = at,
                    )
                }
            }
            is FleetPlan -> planIsCurrent = true
            is MatchAlert -> alertsLog.onAlert(event)
            is MatchAlertResolved -> alertsLog.onResolved(event)
            is ReasoningResult -> alertsLog.onReasoningResult(event)
            is AlertUpgrade -> alertsLog.onAlertUpgrade(event)
            is DetectionProfiles -> onProfilesFromGround()
            is ManualReply -> applyManualReply(event, at)
            is LandAllAck -> applyLandAllAck(event, at)
            is CommandAck -> applyCommandAck(at)
            is ServerMessage -> when (event.level) {
                ServerMessage.Level.ERROR -> addToast(GroundText.shorten(event.message ?: "The ground reported an error."), true, at)
                ServerMessage.Level.WARNING -> addToast(GroundText.shorten(event.message ?: "The ground reported a warning."), false, at)
            }
            else -> Unit // status, plan, profiles, video state and unknown events are not used by this screen.
        }
    }

    /**
     * Each ack answers the oldest outstanding LAND ALL. While a newer tap
     * still waits, an earlier tap's result is shown as a detail (and as a
     * failure banner when it is not a success), and the newer tap keeps its
     * own "sending" and "no reply yet" states.
     */
    private fun applyLandAllAck(ack: LandAllAck, at: Long) {
        val phase: CommandPhase
        val text: String
        val partial = GroundText.landAllPartial(ack)
        when {
            ack.failed -> {
                phase = CommandPhase.FAILED
                text = DisplayText.landAllResult(ack)
            }
            partial != null -> {
                phase = CommandPhase.PARTIAL
                text = partial
            }
            else -> {
                phase = CommandPhase.SUCCEEDED
                text = DisplayText.landAllResult(ack)
            }
        }
        if (landAllOutstanding.isNotEmpty()) landAllOutstanding.removeAt(0)
        val newerStillWaiting = landAllOutstanding.isNotEmpty()
        val current = landAllFeedback
        if (newerStillWaiting && current?.isWaiting == true) {
            landAllFeedback = current.copy(detail = "Earlier LAND ALL: $text")
            if (phase != CommandPhase.SUCCEEDED) addBanner(text, true, at)
            return
        }
        if (current?.phase == CommandPhase.FAILED) {
            // A failure shown for LAND ALL stays visible; this answer is added to it.
            landAllFeedback = current.copy(detail = text, updatedAtMs = at)
            if (phase != CommandPhase.SUCCEEDED) addBanner(text, true, at)
            return
        }
        landAllFeedback = CommandFeedback(
            current?.attempt ?: nextAttempt(), phase, text,
            startedAtMs = current?.startedAtMs ?: at, updatedAtMs = at,
        )
    }

    private fun applyCommandAck(at: Long) {
        if (pendingLegacyLands.isEmpty()) return
        val id = pendingLegacyLands.removeAt(0)
        val current = landFeedback[id]
        if (current != null && current.isWaiting) {
            landFeedback[id] = current.copy(
                phase = CommandPhase.ACCEPTED,
                awaitsFinalAnswer = false, // A ground without a fleet never sends a final answer.
                text = GroundText.legacyLandAccepted(id),
                updatedAtMs = at,
            )
        }
    }

    private fun applyManualReply(reply: ManualReply, at: Long) {
        val id = reply.droneId ?: ""
        val isFinal = reply.kind == ManualReply.Kind.RESULT
        val actionWire = reply.action?.raw ?: "command"
        val isLand: Boolean? = when (reply.action) {
            ManualAction.Land -> true
            ManualAction.ResumeSearch -> false
            else -> null
        }
        val ownCommand = isLand != null && droneFeedback(isLand, id) != null

        if (reply.ok == false) {
            val why = GroundText.manualWhy(reply)
            if (isLand != null && ownCommand) {
                // An answer to the phone's own land or resume.
                val refusal = "${GroundText.shortId(id)}: $why"
                val text = if (isLand) "$refusal. Land the drone with its own controls." else "$refusal."
                val started = droneFeedback(isLand, id)?.startedAtMs ?: at
                setDroneFeedback(isLand, id, CommandFeedback(nextAttempt(), CommandPhase.FAILED, text, startedAtMs = started, updatedAtMs = at))
            } else {
                // Any other failure the ground reports for a drone, for example an orphan
                // auto-land that could not be delivered. Sticky on the card until dismissed.
                val who = id.ifEmpty { "drone" }
                val text = GroundText.droneActionFailed(who, actionWire, why)
                droneAlerts.remove(who)
                droneAlerts[who] = CommandFeedback(nextAttempt(), CommandPhase.FAILED, text, startedAtMs = at, updatedAtMs = at)
                while (droneAlerts.size > MAX_ALERTS) droneAlerts.remove(droneAlerts.keys.first())
                if (board().drones[who] == null) addBanner(text, true, at)
            }
            return
        }
        if (reply.ok != true || isLand == null || id.isEmpty()) return
        val started = droneFeedback(isLand, id)?.startedAtMs ?: at
        val attempt = droneFeedback(isLand, id)?.attempt ?: nextAttempt()
        if (isFinal) {
            val text = if (isLand) GroundText.landAcknowledged(id) else GroundText.searchResumed(id)
            setDroneFeedback(isLand, id, CommandFeedback(attempt, CommandPhase.SUCCEEDED, text, startedAtMs = started, updatedAtMs = at))
        } else if (droneFeedback(isLand, id)?.phase != CommandPhase.SUCCEEDED) {
            val text = if (isLand) GroundText.landAccepted(id) else GroundText.resumeAccepted(id)
            setDroneFeedback(isLand, id, CommandFeedback(attempt, CommandPhase.ACCEPTED, text, startedAtMs = started, updatedAtMs = at))
        }
    }

    /**
     * The link is gone: no answer to anything sent on it can arrive any more.
     * Every command still waiting becomes a visible failure; nothing is resent.
     */
    private fun connectionLost() {
        lossEpoch++
        planIsCurrent = false
        onProfileLinkLost()
        val at = clock.nowMs()
        landAllOutstanding.clear()
        pendingLegacyLands.clear()
        landAllFeedback?.let { f ->
            if (f.phase == CommandPhase.SENDING || f.phase == CommandPhase.HANDED_OVER || f.phase == CommandPhase.NO_REPLY_YET) {
                landAllFeedback = f.copy(phase = CommandPhase.FAILED, text = GroundText.LAND_ALL_CONNECTION_LOST, updatedAtMs = at)
            }
        }
        lose(landFeedback, GroundText.ACTION_LAND, at)
        lose(resumeFeedback, GroundText.ACTION_RESUME, at)
    }

    private fun lose(feedback: MutableMap<String, CommandFeedback>, action: String, at: Long) {
        for ((id, f) in feedback.entries.toList()) {
            if (f.isWaiting) {
                feedback[id] = f.copy(phase = CommandPhase.FAILED, text = GroundText.droneConnectionLost(id, action), updatedAtMs = at)
            }
        }
    }

    private fun addBanner(text: String, isFailure: Boolean, at: Long) {
        var list = listOf(GroundBanner(++idCounter, text, isFailure, at)) + banners
        while (list.size > MAX_BANNERS) {
            // Drop the oldest non-failure first; failures go only when nothing else is left.
            val index = list.indexOfLast { !it.isFailure }
            list = if (index >= 0) list.filterIndexed { i, _ -> i != index } else list.dropLast(1)
        }
        banners = list
    }

    private fun addToast(text: String, isError: Boolean, at: Long) {
        var list = listOf(GroundToast(++idCounter, text, isError, at)) + toasts
        while (list.size > MAX_TOASTS) {
            // Drop the oldest warning first; errors go only when nothing else is left.
            val index = list.indexOfLast { !it.isError }
            list = if (index >= 0) list.filterIndexed { i, _ -> i != index } else list.dropLast(1)
        }
        toasts = list
    }

    // ---------------------------------------------------------------- connection

    private fun onConnectionStatus(status: ConnectionStatus) {
        link = when (status) {
            ConnectionStatus.Idle -> GroundLink.Connecting(1)
            is ConnectionStatus.Connecting -> GroundLink.Connecting(status.attempt)
            is ConnectionStatus.Connected -> GroundLink.Connected
            is ConnectionStatus.Reconnecting ->
                GroundLink.Reconnecting(status.attempt, status.retryInMs / 1000.0, GroundText.dropReason(status.reason))
            is ConnectionStatus.Failed -> when (val reason = status.reason) {
                is FailureReason.AuthRejected -> GroundLink.WrongKey
                is FailureReason.AddressNotAllowed -> GroundLink.AddressRefused(GroundText.shorten(reason.detail))
            }
            ConnectionStatus.ShutDown -> idleLink()
        }
        if (status !is ConnectionStatus.Connected) {
            fleetFresh = false
            connectionLost()
        }
        publish()
    }

    private fun idleLink(): GroundLink = when {
        addressSetting != null && keyState == KeyState.UNAVAILABLE -> GroundLink.StorageUnavailable
        addressSetting == null || keyState != KeyState.SAVED -> GroundLink.NotConfigured
        !enabledSetting -> GroundLink.Off
        inBackground -> GroundLink.Paused
        else -> GroundLink.Stopped
    }

    private suspend fun refreshKeyState() {
        val loaded = secrets.load(SecretName.DASHBOARD_API_KEY)
        keyState = when {
            loaded != null -> KeyState.SAVED
            withContext(ioDispatcher) { secrets.availability() } is SecretAvailability.Unavailable -> KeyState.UNAVAILABLE
            else -> KeyState.NONE
        }
        if (client == null) link = idleLink()
        publish()
    }

    /** Connects when an address and a key are saved and the link is on. Never starts a connection otherwise. */
    private suspend fun connectIfWanted() {
        if (demoOn) return
        if (client != null || !enabledSetting || inBackground) return
        val endpoint = (addressSetting?.let { GroundEndpoint.parseDetailed(it) } as? EndpointParseResult.Ok)?.endpoint
        if (endpoint == null) {
            link = GroundLink.NotConfigured
            publish()
            return
        }
        val myEpoch = epoch
        // The key goes from the secure storage straight into the client and is not kept here.
        val raw = secrets.load(SecretName.DASHBOARD_API_KEY)
        if (epoch != myEpoch || client != null || !enabledSetting || inBackground || demoOn) return
        if (raw == null) {
            refreshKeyState()
            return
        }
        keyState = KeyState.SAVED
        val key = DashboardApiKey.of(raw)
        if (key == null) {
            keyState = KeyState.NONE
            message = "The saved key cannot be used. Enter it again."
            link = idleLink()
            publish()
            return
        }
        startClient(endpoint, key)
    }

    private fun startClient(endpoint: GroundEndpoint, key: DashboardApiKey) {
        if (demoOn) return
        attachClient { cs -> clientFactory(endpoint, key, cs) }
    }

    private fun attachClient(build: (CoroutineScope) -> GroundConsoleClient) {
        // The client does its socket IO, parsing and decoding on its own scope (off the main thread);
        // only this view model's collectors below run on the main thread.
        val cs = CoroutineScope(SupervisorJob() + clientDispatcher)
        val collectors = CoroutineScope(SupervisorJob() + mainDispatcher)
        val c = build(cs)
        client = c
        clientScope = cs
        collectorScope = collectors
        fleetFresh = false
        freshInThisClient = false
        link = GroundLink.Connecting(1)
        // `events` is collected from exactly this one place. Video frames come from `frames`, latest per drone.
        collectors.launch { c.events.collect { if (client === c) handle(it) } }
        collectors.launch { c.frames.collect { if (client === c) onFrames(it) } }
        collectors.launch { c.connection.collect { if (client === c) onConnectionStatus(it) } }
        collectors.launch { c.board.collect { if (client === c) publish() } }
        collectors.launch { c.snapshot.collect { if (client === c) publish() } }
        c.start()
        publish()
    }

    /**
     * Stamps each new frame with the time it arrived (a frame is new when it is not the same object as the
     * one kept for that drone). The client already bounds the count of drones and the size of a frame; a
     * frame the client dropped (link lost) disappears here too, so its image is released.
     */
    private fun onFrames(latest: Map<String, Frame>) {
        val now = clock.nowMs()
        val next = LinkedHashMap<String, ReceivedFrame>(latest.size)
        for ((id, frame) in latest) {
            val before = receivedFrames[id]
            next[id] = if (before != null && before.frame === frame) before else ReceivedFrame(frame, now)
        }
        receivedFrames = next
        publish()
    }

    private fun closeClient() {
        val c = client ?: return
        receivedFrames = emptyMap()
        retainedBoard = displayBoard()
        retainedSnapshot = c.snapshot.value
        val cs = clientScope
        val collectors = collectorScope
        client = null
        clientScope = null
        collectorScope = null
        epoch++
        fleetFresh = false
        connectionLost()
        link = idleLink()
        cleanupScope.launch {
            try {
                c.shutdown()
            } finally {
                cs?.cancel()
                collectors?.cancel()
            }
        }
        publish()
    }

    private fun restart() {
        if (demoOn) {
            // The new settings take effect when demo mode ends.
            publish()
            return
        }
        epoch++
        if (client != null) closeClient()
        link = idleLink()
        publish()
        scope.launch {
            refreshKeyState()
            connectIfWanted()
        }
    }

    /** A different ground: nothing from the old one may stay on screen. */
    private fun forgetGroundState() {
        planIsCurrent = false
        alertsLog.clear()
        receivedFrames = emptyMap()
        profileFeedback = null
        retainedBoard = null
        retainedSnapshot = null
        orphanBase.clear()
        landFeedback.clear()
        resumeFeedback.clear()
        droneAlerts.clear()
        pendingLegacyLands.clear()
        landAllOutstanding.clear()
        banners = emptyList()
        if (client != null) closeClient()
        retainedBoard = null
        retainedSnapshot = null
    }

    private suspend fun runProbe(): String {
        val endpoint = (addressSetting?.let { GroundEndpoint.parseDetailed(it) } as? EndpointParseResult.Ok)?.endpoint
            ?: return "Save the ground station address first."
        val raw = secrets.load(SecretName.DASHBOARD_API_KEY)
        if (raw == null) {
            refreshKeyState()
            return if (keyState == KeyState.UNAVAILABLE) GroundText.SECURE_STORAGE_UNAVAILABLE else "Save the dashboard API key first."
        }
        val key = DashboardApiKey.of(raw) ?: return "The saved key cannot be used. Enter it again."
        val result = try {
            probe(endpoint, key)
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            return "Unreachable: ${e.javaClass.simpleName}. Check the address and that the phone is on the ground's network."
        }
        return when (result) {
            is HealthProbeResult.Ok -> "Reachable, key accepted" + version(result.version) + "."
            is HealthProbeResult.WrongKey -> "Reachable, but the ground refused the key" + version(result.version) + "."
            is HealthProbeResult.Redirected -> "The address answered with a redirect (HTTP ${result.status}); it may not be a FlightRisk ground station."
            is HealthProbeResult.UnexpectedStatus -> "The address answered with HTTP ${result.status}; it may not be a FlightRisk ground station."
            is HealthProbeResult.UnexpectedBody -> "Not a FlightRisk ground station response: ${GroundText.shorten(result.detail)}."
            is HealthProbeResult.AddressNotAllowed -> "Address refused: ${GroundText.shorten(result.detail)}"
            is HealthProbeResult.SecureConnectionFailed ->
                "The secure connection failed: ${GroundText.shorten(result.detail)}. The ground's certificate may be missing, expired, self-signed or issued for another name."
            is HealthProbeResult.Unreachable -> "Unreachable: ${GroundText.shorten(result.detail)}. Check the address and that the phone is on the ground's network."
        }
    }

    private fun version(v: String?): String = v?.let { " (ground version ${GroundText.shorten(it, 40)})" } ?: ""

    // ---------------------------------------------------------------- derived state

    private fun board(): DroneBoard = displayBoard()

    /**
     * The board the cards are built from: the client's own `board`, or, until
     * a fresh `fleet_status` arrives on a new client (after the app was in the
     * background), the last board of the previous client, marked stale.
     */
    private fun displayBoard(): DroneBoard {
        val c = client
        val base = when {
            c == null -> retainedBoard ?: DroneBoard.EMPTY
            !freshInThisClient && retainedBoard != null -> retainedBoard!!
            else -> c.board.value
        }
        return if (isStale(base)) base.copy(stale = true) else base
    }

    private fun isStale(board: DroneBoard): Boolean = !link.isConnected || !fleetFresh || board.stale

    private fun publish() {
        val now = clock.nowMs()
        val board = displayBoard()
        val stale = isStale(board)
        if (stale) {
            if (staleSinceMs == null) staleSinceMs = now
        } else {
            staleSinceMs = null
        }
        val noteText = when {
            !stale -> null
            !link.isConnected -> GroundText.STALE_AFTER_DISCONNECT
            noRecentUpdate -> GroundText.STALE_NO_UPDATE
            else -> GroundText.STALE_REFRESHING
        }
        val snapshot = client?.snapshot?.value ?: retainedSnapshot ?: GroundSnapshot()
        val cards = board.drones.values.map { buildCard(it, board, stale, noteText, now) }
        val connected = cards.count { it.connection == ConnectionState.CONNECTED }
        val legacy = board.drones.values.any { it is DroneRecord.Legacy }
        val readout = buildString {
            append("${cards.size} ${if (cards.size == 1) "drone" else "drones"}, $connected connected")
            if (legacy) append(" (no fleet: local drone only)")
            if (stale) append(" (last values)")
        }
        val inDemo = demoOn
        fun shown(text: String): String = if (inDemo) DemoText.prefixed(text) else text
        _ui.value = GroundUiState(
            link = link,
            isStateStale = stale,
            staleNote = noteText,
            cards = cards,
            readout = readout,
            landAll = landAllFeedback?.let { f -> if (inDemo) f.copy(text = shown(f.text), detail = f.detail?.let(::shown)) else f },
            landAllText = landAllFeedback?.let { shown(feedbackText(it, now)) },
            banners = if (inDemo) banners.map { it.copy(text = shown(it.text)) } else banners,
            toasts = if (inDemo) toasts.map { it.copy(text = shown(it.text)) } else toasts,
            appearanceOnly = snapshot.status?.appearanceOnly == true,
            connection = ConnectionCardState(
                savedAddress = addressSetting,
                addressError = addressError,
                keyState = keyState,
                enabled = enabledSetting,
                message = message,
                testing = testing,
                testResult = testResult,
                demo = demoOn,
            ),
            board = board,
            snapshot = snapshot,
            planIsCurrent = planIsCurrent,
            profileFeedback = profileFeedback?.let { f -> if (inDemo) f.copy(text = shown(f.text)) else f },
            videoTiles = VideoTileLogic.makeTiles(receivedFrames, now, snapshot.streamVideo, stale),
            videoOffAtGround = snapshot.streamVideo == false && !stale,
            alerts = alertsLog.ui(stale),
            demo = demoOn,
            demoProblem = demoProblem,
            demoNotice = demoNotice,
            demoEndedNote = demoEndedNote,
        )
    }

    /** A feedback text with its age once it is no longer in flight. */
    private fun feedbackText(f: CommandFeedback, now: Long): String = when (f.phase) {
        CommandPhase.SENDING, CommandPhase.HANDED_OVER -> f.text
        else -> f.text + GroundText.age((now - f.updatedAtMs) / 1000.0)
    }

    private fun buildCard(record: DroneRecord, board: DroneBoard, stale: Boolean, staleText: String?, now: Long): DroneCardUi {
        val id = record.id
        fun lastValue(text: String) =
            if (stale && text != DisplayText.DASH && text.isNotEmpty()) text + GroundText.LAST_VALUE_SUFFIX else text

        val display = board.controlMode(id)
        val held = display as? ControlModeDisplay.Held
        val hasCountdown = held?.orphanLandInS != null
        val controlText = when {
            held != null && held.orphanLandInS != null -> ControlModeDisplay.Held(countdown(id, held.orphanLandInS!!, now)).text
            else -> display?.text.orEmpty()
        }
        val chip = when (display) {
            is ControlModeDisplay.Held -> "HELD"
            ControlModeDisplay.Manual -> "MANUAL"
            else -> null
        }
        val battery: Double?
        val height: Double?
        val lane: String?
        val notice: String?
        when (record) {
            is DroneRecord.Fleet -> {
                battery = record.drone.battery
                height = record.drone.heightCm
                val i = record.drone.laneIndex
                val n = record.drone.lanesTotal
                lane = if (i != null && n != null) "$i/$n" else null
                notice = DisplayText.droneLinkDown(record.drone)?.let { GroundText.shorten(it) }
            }
            is DroneRecord.Legacy -> {
                battery = record.drone.battery
                height = record.drone.height
                lane = null
                notice = null
            }
        }
        val connection = record.connection
        val land = landFeedback[id]
        val resume = resumeFeedback[id]
        val alert = droneAlerts[id]
        return DroneCardUi(
            id = id,
            connection = connection,
            linkChipText = if (stale) "LAST: ${connection.label}" else connection.label,
            linkChipGreyed = stale,
            controlChip = chip,
            controlText = lastValue(controlText),
            isHeldWithCountdown = hasCountdown,
            batteryText = lastValue(battery?.let { DisplayText.fmt(it) + "%" } ?: DisplayText.DASH),
            altitudeText = lastValue(height?.let { DisplayText.fmt(it) + " cm" } ?: DisplayText.DASH),
            laneText = lane?.let { lastValue(it) },
            linkNotice = notice,
            isPartial = (record as? DroneRecord.Fleet)?.partial == true,
            isStateStale = stale,
            isValuesStale = stale || connection.isStale,
            staleNote = if (stale) staleText else if (connection.isStale) ConnectionState.STALE_NOTE else null,
            canResume = !stale && link.isConnected && board.canResumeSearch(id) && !connection.isStale,
            landFeedback = land,
            resumeFeedback = resume,
            alert = alert,
            landFeedbackText = land?.let { demoShown(feedbackText(it, now)) },
            resumeFeedbackText = resume?.let { demoShown(feedbackText(it, now)) },
            alertText = alert?.let { demoShown(feedbackText(it, now)) },
            isDemo = demoOn,
        )
    }

    /** [text] as the screen shows it: with "DEMO: " in front while demo mode is on. */
    private fun demoShown(text: String): String = if (demoOn) DemoText.prefixed(text) else text

    /**
     * The HELD countdown, counted down locally from the latest server value as
     * the contract says, and frozen while the state is stale.
     */
    private fun countdown(id: String, received: Double, now: Long): Double {
        val base = orphanBase[id]
        if (base == null || base.first != received) return received.coerceAtLeast(0.0)
        val reference = staleSinceMs ?: now
        return DisplayText.orphanCountdown(base.first, base.second, reference)
    }

    // ---------------------------------------------------------------- tests

    /** True while the one second ticker runs. For tests. */
    internal fun tickerRunning(): Boolean = tickerJob?.isActive == true

    /** The client's own connection status, or null without a client. For tests. */
    internal fun currentClientStatusForTest(): ConnectionStatus? = client?.connection?.value

    /** Waits for the operations launched so far (key reads, saves, connects). For tests. */
    internal suspend fun awaitIdle() {
        val jobs = scope.coroutineContext[Job]!!.children.filter { it !== tickerJob }.toList()
        jobs.joinAll()
    }

    companion object {
        /** How long a command may wait for the ground's answer before "no reply yet" shows. */
        const val REPLY_TIMEOUT_MS = 5_000L

        /** How long the ground waits for an edge before it reports `no_reply` itself (`MANUAL_REPLY_TIMEOUT_S`). */
        const val MANUAL_REPLY_TIMEOUT_MS = 4_000L
        const val SUCCESS_FADE_MS = 10_000L

        /** The ground's LAND ALL deadline is 35 s; a tap unanswered for this long will never be answered. */
        const val LAND_ALL_EXPIRES_MS = 40_000L

        /** No `fleet_status` for this long while connected marks the state stale. */
        const val FRESHNESS_TIMEOUT_MS = 30_000L
        const val MAX_ALERTS = 100
        const val DRONE_SUCCESS_FADE_MS = 60_000L
        const val WARNING_FADE_MS = 8_000L
        const val MAX_BANNERS = 10
        const val MAX_TOASTS = 5
        private const val TICK_MS = 1_000L

        /**
         * The events this screen may emit: the five state requests the client
         * sends itself, LAND ALL, per-drone land and resume, and the two
         * detection profile selections of the profiles card. That is every event
         * in the contract's allow-list today, so the policy equals it; it is
         * still built by narrowing, so it can never allow more than the
         * contract does. It has no ceiling, reasoning or plan control because
         * the contract has none.
         */
        val EMIT_POLICY: EmitPolicy = EmitPolicy.CONTRACT.narrowedTo(
            setOf(
                "get_fleet_status", "get_detection_profiles", "get_ceiling_settings", "get_reasoning_provider",
                "get_manual_status", "land_all", "drone_command", "manual_resume_search",
                "set_detection_profile", "set_fleet_detection_profile",
            ),
        )

        private fun defaultClient(endpoint: GroundEndpoint, key: DashboardApiKey, scope: CoroutineScope) =
            GroundConsoleClient(endpoint, key, scope, emitPolicy = EMIT_POLICY, logSink = AndroidGroundLog())

        /** Reads the packaged demo mission off the main thread; null when it is missing or not usable. */
        suspend fun loadDemoScenario(context: Context): DemoScenario? = withContext(Dispatchers.IO) {
            try {
                context.assets.open(DemoScenario.ASSET_PATH).use { input ->
                    DemoScenario.parse(input.readBytes().decodeToString())
                }
            } catch (_: java.io.IOException) {
                null
            }
        }

        /** Builds the one app-wide instance (owned by the activity's ViewModel store). */
        fun factory(context: Context): ViewModelProvider.Factory {
            val app = context.applicationContext
            return object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T =
                    GroundConsoleViewModel(
                        AppSecrets.store(app),
                        SharedPrefsGroundSettings(app),
                        demoScenario = { loadDemoScenario(app) },
                    ) as T
            }
        }
    }
}
