package com.flightrisk.app.transport

import android.content.Context
import android.content.SharedPreferences
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import com.flightrisk.app.config.AppSecrets
import com.flightrisk.app.config.SingleInstance
import com.flightrisk.app.ground.AndroidGroundLog
import com.flightrisk.app.ground.EndpointParseResult
import com.flightrisk.app.ground.GroundEndpoint
import com.flightrisk.app.ground.SecretName
import com.flightrisk.app.ground.SecretStore
import com.flightrisk.app.pipeline.DetectionPublisher
import com.flightrisk.app.pipeline.PublishedFrame
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The saved choice for the ground station link: on or off, and the address
 * the user consented to. Not secret; the edge token is in [SecretStore].
 */
interface GroundLinkPrefs {
    fun enabled(): Boolean
    fun address(): String
    fun save(enabled: Boolean, address: String)
}

/**
 * [GroundLinkPrefs] in the `flightrisk_ground_link` preferences file, which
 * is excluded from backup and device transfer (`backup_rules.xml`,
 * `data_extraction_rules.xml`): consent given on this phone must not be
 * restored to another one.
 */
class SharedGroundLinkPrefs(private val prefs: SharedPreferences) : GroundLinkPrefs {
    constructor(context: Context) : this(context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE))

    override fun enabled(): Boolean = try { prefs.getBoolean(ENABLED_KEY, false) } catch (_: RuntimeException) { false }
    override fun address(): String = try { prefs.getString(ADDRESS_KEY, "").orEmpty() } catch (_: RuntimeException) { "" }

    override fun save(enabled: Boolean, address: String) {
        prefs.edit().putBoolean(ENABLED_KEY, enabled).putString(ADDRESS_KEY, address).commit()
    }

    companion object {
        /** Excluded from backup and device transfer. */
        const val PREFS_NAME = "flightrisk_ground_link"
        const val ENABLED_KEY = "flightrisk_ground_link_enabled"
        const val ADDRESS_KEY = "flightrisk_ground_link_address"
    }
}

/** What Settings and the Search screen show about the link. Holds no secret. */
data class GroundLinkUiState(
    /** The user turned the link on (and consented to [address]). */
    val enabled: Boolean = false,
    /** The saved ground station address, as `host:port`, or empty. */
    val address: String = "",
    /** Why the last address was refused, or null. */
    val addressProblem: String? = null,
    val hasToken: Boolean = false,
    /** The result of the last token action, or null. Never contains the token. */
    val tokenMessage: String? = null,
    val status: EdgeLinkStatus = EdgeLinkStatus.Off,
    /** True while the connection loop is running (link on, app in view, a search running). */
    val running: Boolean = false,
    /** True while the ground has asked for video, so the whole camera view goes out too. */
    val sendingCameraView: Boolean = false,
)

/** The words the link shows. */
object GroundLinkText {
    const val TOKEN_REJECTED = "The ground rejected the edge token. Fix it in Settings."

    const val DISCLOSURE_TITLE = "Send detections to a ground station?"

    const val DISCLOSURE =
        "While a search is running, this phone will send what its camera detects to the ground station address you entered: " +
            "where each person appears in the picture (box positions) and how sure the phone is (scores). No pictures of people " +
            "and no face data are sent. If the ground station asks for video, the phone also sends a small picture of the whole " +
            "camera view, which can show people. The ground station cannot yet match these detections to a search target, so use " +
            "this mode for testing and mapping only. The link is not encrypted, and this version of the app does not support " +
            "wss://, so use it only on a network you trust. The address you entered is remembered, not the network: on a different " +
            "Wi-Fi, another device with the same address would receive what is sent, including your edge token. " +
            "The phone sends nothing else and cannot control anything on the ground station. You can turn this off at any time."

    const val NEEDS_TOKEN = "Save the edge token from the ground station first."
    const val STORAGE_UNAVAILABLE = "Secure storage unavailable on this device"
    const val PORT_HINT = "The edge port is usually 9000; 5555 is the dashboard."

    fun statusLine(state: GroundLinkUiState): String {
        if (!state.enabled) return "Off. Nothing is sent."
        return when (val s = state.status) {
            EdgeLinkStatus.Off -> "On. Connects to ${state.address} while a search is running."
            is EdgeLinkStatus.Connecting -> "Connecting to ${state.address}..."
            EdgeLinkStatus.Connected -> "Connected. Sending detections to ${state.address}."
            is EdgeLinkStatus.Reconnecting -> "Connection lost (${s.reason}). Trying again in ${(s.retryInMs + 999) / 1000} s."
            EdgeLinkStatus.TokenRejected -> TOKEN_REJECTED
            EdgeLinkStatus.NeedsToken -> NEEDS_TOKEN
            EdgeLinkStatus.StorageUnavailable -> STORAGE_UNAVAILABLE
            is EdgeLinkStatus.AddressRefused -> "Not connected: ${s.reason}"
        }
    }

    /** The banner on the Search screen, or null when there is nothing to say. */
    fun indicator(state: GroundLinkUiState): GroundLinkIndicator? {
        if (!state.enabled) return null
        return when (state.status) {
            EdgeLinkStatus.Connected ->
                if (!state.running) {
                    null
                } else if (state.sendingCameraView) {
                    GroundLinkIndicator("Sending detections and camera view to ${state.address}", sending = true)
                } else {
                    GroundLinkIndicator("Sending detections to ${state.address}", sending = true)
                }
            EdgeLinkStatus.TokenRejected -> GroundLinkIndicator(TOKEN_REJECTED, sending = false)
            EdgeLinkStatus.NeedsToken -> GroundLinkIndicator(NEEDS_TOKEN, sending = false)
            EdgeLinkStatus.StorageUnavailable -> GroundLinkIndicator(STORAGE_UNAVAILABLE, sending = false)
            is EdgeLinkStatus.AddressRefused -> GroundLinkIndicator(statusLine(state), sending = false)
            else -> null
        }
    }
}

/**
 * Proof that the user accepted the disclosure for [addressText]. Only the
 * disclosure dialog creates one (the constructor is internal to the app), and
 * [GroundLinkController.enable] refuses to turn the link on without one that
 * matches the address.
 */
class DisclosureAccepted internal constructor(val addressText: String)

/** [text] for the Search screen; [sending] is true only while detections go out. */
data class GroundLinkIndicator(val text: String, val sending: Boolean)

/**
 * Owns the ground station link: the saved choice, the edge token, and one
 * [EdgePublisher] that exists only while the link is on, the app is in view
 * and a search is running. Turning the link on is the user's act; nothing
 * starts from a saved token alone, and a changed address turns the link off
 * so the user consents to the new destination.
 *
 * The edge token is read from [secrets] when a connection is made and never
 * kept here, so no state, log line or `toString` of this class holds it.
 */
class GroundLinkController(
    private val prefs: GroundLinkPrefs,
    private val secrets: SecretStore,
    private val scope: CoroutineScope,
    private val publisherFactory: (GroundEndpoint) -> EdgePublisher,
) {
    private val lock = Any()
    private val _state = MutableStateFlow(GroundLinkUiState())

    val state: StateFlow<GroundLinkUiState> = _state.asStateFlow()

    private var publisher: EdgePublisher? = null
    private var statusJob: Job? = null
    private var foreground = false
    private var searching = false

    init {
        val savedAddress = prefs.address()
        val valid = EdgeEndpoint.parseOrNull(savedAddress)
        // A saved "on" without a usable address is treated as off.
        val enabled = prefs.enabled() && valid != null
        _state.value = GroundLinkUiState(enabled = enabled, address = valid?.let(::label) ?: "")
        scope.launch { refreshTokenPresence() }
    }

    /** What the pipeline publishes to. It follows this controller's current publisher. */
    val detectionPublisher: DetectionPublisher = object : DetectionPublisher {
        override fun wantsFrame(): Boolean = publisher?.wantsFrame() ?: false
        override fun publish(frame: PublishedFrame) {
            publisher?.publish(frame)
        }

        override fun onSearchStarted() = setSearching(true)
        override fun onSearchStopped() = setSearching(false)
        override fun toString(): String = "GroundLinkPublisher"
    }

    /** Starts and stops the link with the activity: it stops when the app leaves the screen. */
    val lifecycleObserver: DefaultLifecycleObserver = object : DefaultLifecycleObserver {
        override fun onStart(owner: LifecycleOwner) = setForeground(true)
        override fun onStop(owner: LifecycleOwner) {
            // A rotation recreates the activity; the link goes down only when the app really leaves.
            if ((owner as? android.app.Activity)?.isChangingConfigurations == true) return
            setForeground(false)
        }
    }

    /**
     * Turns the link on for [addressText] after the user accepted the disclosure
     * for that address ([accepted], created by the dialog).
     * Returns false and shows the reason in [GroundLinkUiState.addressProblem]
     * when the address is refused.
     */
    fun enable(addressText: String, accepted: DisclosureAccepted?): Boolean {
        if (accepted == null || accepted.addressText != addressText) {
            _state.update { it.copy(addressProblem = "Read and accept the disclosure first.") }
            return false
        }
        val parsed = EdgeEndpoint.parse(addressText)
        if (parsed !is EndpointParseResult.Ok) {
            _state.update { it.copy(addressProblem = (parsed as EndpointParseResult.Invalid).reason) }
            return false
        }
        val address = label(parsed.endpoint)
        synchronized(lock) {
            prefs.save(true, address)
            _state.update { it.copy(enabled = true, address = address, addressProblem = null) }
            reconcileLocked()
        }
        return true
    }

    /** Shows why [addressText] is refused (or clears the message). Changes nothing else. */
    fun checkAddress(addressText: String) {
        val parsed = EdgeEndpoint.parse(addressText)
        _state.update { it.copy(addressProblem = (parsed as? EndpointParseResult.Invalid)?.reason) }
    }

    /** Turns the link off and closes the connection. */
    fun disable() {
        synchronized(lock) {
            prefs.save(false, _state.value.address)
            _state.update { it.copy(enabled = false, addressProblem = null) }
            reconcileLocked()
        }
    }

    /**
     * Saves a new address. If the link was on and the address differs, the
     * link turns off: the consent covered the old address only.
     */
    fun changeAddress(addressText: String): Boolean {
        val parsed = EdgeEndpoint.parse(addressText)
        if (parsed !is EndpointParseResult.Ok) {
            _state.update { it.copy(addressProblem = (parsed as EndpointParseResult.Invalid).reason) }
            return false
        }
        val address = label(parsed.endpoint)
        synchronized(lock) {
            val changed = address != _state.value.address
            prefs.save(if (changed) false else _state.value.enabled, address)
            _state.update {
                it.copy(
                    address = address,
                    addressProblem = null,
                    enabled = if (changed) false else it.enabled,
                    tokenMessage = if (changed && it.enabled) "The address changed, so the link is off. Turn it on again to confirm." else it.tokenMessage,
                )
            }
            reconcileLocked()
        }
        return true
    }

    /** Saves the edge token (trimmed; the caller clears its field) and reconnects with it. */
    fun saveToken(raw: String) {
        val token = normalizedToken(raw)
        if (token == null) {
            _state.update { it.copy(tokenMessage = "The token was not saved. Use printable characters without spaces, up to $MAX_TOKEN_LENGTH long.") }
            return
        }
        scope.launch {
            val ok = secrets.save(SecretName.EDGE_TOKEN, token)
            _state.update { it.copy(tokenMessage = if (ok) "Token saved." else "The token was not saved: secure storage is unavailable.") }
            refreshTokenPresence()
            if (ok) restart()
        }
    }

    /** Deletes the saved edge token. */
    fun removeToken() {
        scope.launch {
            val ok = secrets.save(SecretName.EDGE_TOKEN, null)
            _state.update { it.copy(tokenMessage = if (ok) "Token removed." else "The token could not be removed: secure storage is unavailable.") }
            refreshTokenPresence()
            if (ok) restart()
        }
    }

    fun setForeground(value: Boolean) {
        synchronized(lock) {
            foreground = value
            reconcileLocked()
        }
    }

    fun setSearching(value: Boolean) {
        synchronized(lock) {
            searching = value
            reconcileLocked()
        }
    }

    private suspend fun refreshTokenPresence() {
        val has = try { secrets.load(SecretName.EDGE_TOKEN) != null } catch (_: Exception) { false }
        _state.update { it.copy(hasToken = has) }
    }

    private fun restart() {
        synchronized(lock) {
            stopPublisherLocked(keepProblem = false)
            reconcileLocked()
        }
    }

    private fun reconcileLocked() {
        val s = _state.value
        val endpoint = if (s.enabled) EdgeEndpoint.parseOrNull(s.address) else null
        val wanted = endpoint != null && foreground && searching
        if (!wanted) {
            stopPublisherLocked(keepProblem = true)
            return
        }
        if (publisher?.endpoint == endpoint) return
        stopPublisherLocked(keepProblem = false)
        val p = publisherFactory(endpoint!!)
        publisher = p
        statusJob = scope.launch {
            launch {
                p.status.collect { status ->
                    // Only the current publisher may write the state; a late update after stop is dropped.
                    synchronized(lock) { if (publisher === p) _state.update { it.copy(status = status) } }
                }
            }
            launch {
                p.thumbnailsOn.collect { on ->
                    synchronized(lock) { if (publisher === p) _state.update { it.copy(sendingCameraView = on) } }
                }
            }
        }
        _state.update { it.copy(running = true) }
        p.start()
    }

    private fun stopPublisherLocked(keepProblem: Boolean) {
        val p = publisher ?: run {
            _state.update { if (keepProblem) it.copy(running = false) else it.copy(running = false, sendingCameraView = false, status = EdgeLinkStatus.Off) }
            return
        }
        statusJob?.cancel()
        statusJob = null
        p.stop()
        publisher = null
        _state.update {
            val problem = it.status is EdgeLinkStatus.TokenRejected || it.status is EdgeLinkStatus.AddressRefused ||
                it.status is EdgeLinkStatus.NeedsToken || it.status is EdgeLinkStatus.StorageUnavailable
            it.copy(running = false, sendingCameraView = false, status = if (keepProblem && problem) it.status else EdgeLinkStatus.Off)
        }
    }

    override fun toString(): String = "GroundLinkController(enabled=${_state.value.enabled}, running=${_state.value.running})"

    companion object {
        const val MAX_TOKEN_LENGTH = 512

        /** The token trimmed, or null when it is empty, too long or has a space or control character. */
        fun normalizedToken(raw: String): String? {
            val t = raw.trim()
            if (t.isEmpty() || t.length > MAX_TOKEN_LENGTH) return null
            if (t.any { it.code < 0x21 || it.code > 0x7e }) return null
            return t
        }

        /** The address as shown and saved: `host:port`, IPv6 in brackets. */
        fun label(endpoint: GroundEndpoint): String = endpoint.hostHeader
    }
}

/** The app's one [GroundLinkController]. */
object GroundLink {
    private val instance = SingleInstance<GroundLinkController>()

    fun controller(context: Context): GroundLinkController = instance.get {
        val app = context.applicationContext
        val secrets = AppSecrets.store(app)
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        GroundLinkController(
            prefs = SharedGroundLinkPrefs(app),
            secrets = secrets,
            scope = scope,
            publisherFactory = { endpoint ->
                EdgePublisher(
                    endpoint = endpoint,
                    tokenProvider = { withContext(Dispatchers.IO) { secrets.lookup(SecretName.EDGE_TOKEN) } },
                    scope = scope,
                    port = EdgeSocketPort(),
                    logSink = AndroidGroundLog(),
                )
            },
        )
    }
}
