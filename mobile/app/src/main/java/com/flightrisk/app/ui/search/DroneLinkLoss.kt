package com.flightrisk.app.ui.search

/**
 * What a lost drone connection does to a running search: it stops through the same path as the
 * Stop button, so the screen never keeps showing "searching" with no frames arriving.
 */
object DroneLinkLoss {
    /** Runs [stopSearch] when a search is running. Returns true if it ran. */
    fun stopSearchIfRunning(searchRunning: Boolean, stopSearch: () -> Unit): Boolean {
        if (!searchRunning) return false
        stopSearch()
        return true
    }
}
