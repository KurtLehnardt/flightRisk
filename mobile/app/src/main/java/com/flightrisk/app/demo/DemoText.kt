package com.flightrisk.app.demo

/** The words of demo mode, in one place so the screens and the tests agree. */
object DemoText {
    /** The banner on every screen while demo mode is on. */
    const val BANNER = "DEMO – no real drones"
    const val BANNER_DETAIL = "A recorded mission on this phone. Nothing is sent to any drone or ground station."

    const val TRY_DEMO = "Try demo"
    const val TRY_DEMO_DETAIL = "Explore the app with a recorded mission. No photo, drone or ground station needed."

    const val SETTINGS_TITLE = "Demo mode"
    const val SETTINGS_SWITCH = "Show a recorded demo mission"
    const val SETTINGS_DETAIL =
        "Replays a recorded search on the Ground tab and uses a drawn placeholder target. Commands are answered on " +
            "this phone and never sent. Leaving demo mode clears everything the demo showed."

    const val CONFIRM_TITLE = "Disconnect from the ground station?"

    fun confirmText(host: String): String =
        "Demo mode disconnects this phone from the ground station at $host. While the demo runs, this phone shows " +
            "no real drones and cannot land them. Use LAND ALL on the ground laptop for the real fleet."

    const val CONFIRM_ACCEPT = "Disconnect and start demo"
    const val CONFIRM_CANCEL = "Stay connected"

    const val BLOCKED_TITLE = "Demo mode not started"
    const val BLOCKED_BY_SEARCH = "Stop the search and disconnect the drone on the Search tab before you start demo mode."
    const val UNAVAILABLE = "The demo mission could not be loaded on this phone."

    /** The Ground screen's link line while the demo runs. */
    const val GROUND_LINK = "Demo ground: a recorded mission on this phone. No real drones; nothing is sent."
    const val GROUND_PILL = "DEMO"
    const val CONNECTION_NOTE =
        "Demo mode is on. These settings are kept for your real ground station and are not used until you leave " +
            "demo mode in Settings."
    const val TEST_IN_DEMO = "Leave demo mode in Settings to test the real ground station."

    const val LAND_ALL_ACKNOWLEDGED = "Demo: LAND ALL was acknowledged on this phone. Nothing was sent to any drone or ground station."
    const val PROFILE_ACKNOWLEDGED = "Demo: the profile choice was acknowledged on this phone. Nothing was sent."

    fun landAcknowledged(droneId: String): String =
        "Demo: Land for ${shortId(droneId)} was acknowledged on this phone. Nothing was sent to any drone or ground station."

    fun resumeAcknowledged(droneId: String): String =
        "Demo: Resume search for ${shortId(droneId)} was acknowledged on this phone. Nothing was sent."

    // Search tab
    const val SEARCH_NOTE = "Demo search: a drawn scene, not the camera. Nothing is analysed or sent."
    const val DRONE_NOT_SENT = "Demo mode: no real drone. The command was acknowledged on this phone and nothing was sent."

    private fun shortId(id: String): String = if (id.length <= 40) id else id.take(39) + "…"
}
