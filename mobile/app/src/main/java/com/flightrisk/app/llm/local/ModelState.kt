package com.flightrisk.app.llm.local

sealed class ModelState {
    data object Idle : ModelState()
    data class Downloading(val progress: Float) : ModelState()
    data object Downloaded : ModelState()
    data object Loading : ModelState()
    data object Ready : ModelState()
    data class Error(val message: String) : ModelState()

    val isTerminal: Boolean
        get() = this is Idle || this is Downloaded || this is Ready || this is Error
}
