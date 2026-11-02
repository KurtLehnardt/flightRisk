package com.flightrisk.app.llm

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** A consent dialog waiting for the user's answer. */
data class ConsentRequest(val selectCloud: Boolean)

/**
 * Everything the reasoning section of Settings and the Search banner show.
 * Holds no key material: the key field's text stays inside the composable and
 * is cleared after saving.
 *
 * @property status The selector's resolved state (selected, active, reasons, notice).
 * @property consent The consent dialog to show, or null.
 * @property message A one-off message after saving or removing the key.
 */
data class ReasoningUiState(
    val status: ReasoningStatus = ReasoningStatus(),
    val consent: ConsentRequest? = null,
    val message: String? = null,
) {
    val selected: ReasoningProvider get() = status.selected
    val cloudOptIn: Boolean get() = status.cloudOptIn
    val hasSavedKey: Boolean get() = status.keyState == ClaudeKeyState.FOUND
    val secureStorageUnavailable: Boolean get() = status.keyState == ClaudeKeyState.UNAVAILABLE
    val isCloudActive: Boolean get() = status.isCloudActive

    /** The key field and its Save button appear only after the opt-in. */
    val showKeyField: Boolean get() = cloudOptIn

    /** "Remove saved key" appears whenever a key is saved, opted in or not. */
    val showRemoveKey: Boolean get() = hasSavedKey
}

/** User actions in the reasoning section. */
sealed interface ReasoningAction {
    data class PickProvider(val provider: ReasoningProvider) : ReasoningAction
    data class SetCloudOptIn(val enabled: Boolean) : ReasoningAction
    data object ConfirmConsent : ReasoningAction
    data object DismissConsent : ReasoningAction

    /** Save a pasted key. Not a data class, so [toString] never shows the input. */
    class SaveKey(val input: String) : ReasoningAction {
        override fun toString(): String = "SaveKey(<redacted>)"
    }

    data object RemoveKey : ReasoningAction
    data object DismissMessage : ReasoningAction
}

/**
 * Applies the reasoning actions from Settings: the consent decisions come from
 * [ReasoningSettings], key changes go through [CloudReasoningGate] on [io], and
 * every change ends with a selector refresh so the indicator and the fallback
 * notice follow at once. Plain Kotlin, so the decision flow is unit-tested.
 */
class ReasoningController(
    private val settings: ReasoningSettings,
    private val gate: CloudReasoningGate,
    private val selector: LlmSelector,
    private val scope: CoroutineScope,
    private val io: CoroutineDispatcher = Dispatchers.IO,
) {
    private val local = MutableStateFlow(ReasoningUiState())

    /** The state for the UI. */
    val ui: StateFlow<ReasoningUiState> =
        combine(selector.status, local) { status, l -> l.copy(status = status) }
            .stateIn(scope, SharingStarted.Eagerly, ReasoningUiState(status = selector.status.value))

    /**
     * Runs the one-time key migration and the first key lookup off the main
     * thread. It never changes the opt-in or the provider.
     */
    suspend fun start(migrate: () -> Unit) {
        withContext(io) {
            migrate()
            selector.refreshWithKeyLookup()
        }
    }

    fun onAction(action: ReasoningAction) {
        when (action) {
            is ReasoningAction.PickProvider -> handle(settings.requestProvider(action.provider))
            is ReasoningAction.SetCloudOptIn -> {
                if (!action.enabled) local.update { it.copy(message = null) }
                handle(settings.requestCloudOptIn(action.enabled))
            }
            ReasoningAction.ConfirmConsent -> {
                val pending = local.value.consent ?: return
                settings.confirmCloudConsent(pending.selectCloud)
                local.update { it.copy(consent = null) }
                selector.refresh()
                scope.launch { withContext(io) { selector.refreshWithKeyLookup() } }
            }
            ReasoningAction.DismissConsent -> local.update { it.copy(consent = null) }
            is ReasoningAction.SaveKey -> saveKey(action.input)
            ReasoningAction.RemoveKey -> removeKey()
            ReasoningAction.DismissMessage -> local.update { it.copy(message = null) }
        }
    }

    private fun handle(decision: ConsentDecision) {
        when (decision) {
            ConsentDecision.Applied -> selector.refresh()
            is ConsentDecision.AskForConsent -> local.update {
                if (it.consent != null) it else it.copy(consent = ConsentRequest(decision.selectCloud))
            }
        }
    }

    private fun saveKey(input: String) {
        if (!settings.cloudOptIn || ReasoningSettings.normalizedKey(input) == null) return
        scope.launch {
            val saved = withContext(io) {
                val ok = gate.saveKey(input)
                selector.refresh()
                ok
            }
            val message = when {
                saved -> "Claude API key saved."
                gate.keyState == ClaudeKeyState.UNAVAILABLE ->
                    "${ReasoningReason.SECURE_STORAGE_UNAVAILABLE}. The key was not saved and cloud reasoning stays off."
                else -> "The key could not be saved."
            }
            local.update { it.copy(message = message) }
        }
    }

    private fun removeKey() {
        scope.launch {
            val removed = withContext(io) {
                val ok = gate.removeKey()
                selector.refresh()
                ok
            }
            val message = when {
                removed -> "Saved Claude API key removed."
                gate.keyState == ClaudeKeyState.UNAVAILABLE ->
                    "${ReasoningReason.SECURE_STORAGE_UNAVAILABLE}. The key could not be removed."
                else -> "The key could not be removed."
            }
            local.update { it.copy(message = message) }
        }
    }

    override fun toString(): String = "ReasoningController(${ui.value.status})"
}
