package com.flightrisk.app.demo

import com.flightrisk.app.ui.ground.GroundText

/** The words of demo mode, in one place so the screens and the tests agree. */
object DemoText {
    /** The banner on every screen while demo mode is on. */
    const val BANNER = "DEMO – no real drones"
    const val BANNER_DETAIL = "A recorded mission on this phone. Nothing is sent to any drone or ground station."

    const val TRY_DEMO = "Try demo"
    const val TRY_DEMO_DETAIL = "A recorded mission. No photo or drone needed."

    const val SETTINGS_TITLE = "Demo mode"
    const val SETTINGS_SWITCH = "Demo mission"
    const val SETTINGS_DETAIL =
"Replays a recorded search. Nothing is sent. Turns the ground link off."

    /**
     * Every result, banner and message the Ground screen shows in demo mode starts with this, so a demo
     * answer is never taken for a real one.
     */
    const val PREFIX = "DEMO: "

    /** [text] with [PREFIX], once. */
    fun prefixed(text: String): String = if (text.startsWith(PREFIX)) text else PREFIX + text

    /** The LAND ALL button's text in demo mode. */
    const val LAND_ALL_LABEL = "LAND ALL (demo)"

    /** What TalkBack reads for LAND ALL in demo mode. */
    const val LAND_ALL_DESCRIPTION =
        "LAND ALL (demo). Demo: lands only the simulated drones on this phone. Nothing is sent."

    /** What TalkBack reads for LAND ALL with a real ground. */
    const val LAND_ALL_DESCRIPTION_REAL = "Land all drones"

    /** What TalkBack reads for a demo drone's card title. */
    fun droneDescription(displayId: String): String = "${PREFIX}simulated drone $displayId"

    /** What TalkBack reads for a demo drone's Land button. */
    fun landDescription(title: String, displayId: String): String =
        "$PREFIX$title $displayId. Lands only the simulated drone on this phone. Nothing is sent."

    /** What TalkBack reads for a demo drone's Resume search button. */
    fun resumeDescription(displayId: String): String =
        "${PREFIX}Resume the search of $displayId. Only the simulated drone on this phone. Nothing is sent."

    const val CONFIRM_TITLE = "Switch off the ground link?"

    /**
     * The confirmation before demo mode replaces a real ground link. [host] is the ground's address,
     * or null when only state from an earlier connection is shown.
     */
    fun confirmText(host: String?): String {
        val where = if (host != null) "the ground station at $host" else "the ground station"
        return "Demo mode disconnects this phone from $where and switches the ground link off. While the demo " +
            "runs, this phone shows no real drones and cannot land them. Use LAND ALL on the ground laptop for " +
            "the real fleet. After the demo, turn the link on again on the Ground tab to reconnect."
    }

    const val CONFIRM_ACCEPT = "Switch off and start demo"
    const val CONFIRM_CANCEL = "Stay connected"

    const val BLOCKED_TITLE = "Demo mode not started"
    const val BLOCKED_BY_SEARCH = "Stop the search and disconnect the drone on the Search tab before you start demo mode."
    const val UNAVAILABLE = "The demo mission could not be loaded on this phone."

    /** A real drone reads as flying: demo mode would hide it while it is in the air. */
    fun blockedByFlying(ids: List<String>): String {
        val named = ids.take(3).joinToString(", ") { GroundText.shortId(it) } + if (ids.size > 3) ", …" else ""
        return "The real ground station reports a drone in the air ($named). Demo mode starts only when the " +
            "Ground tab shows every real drone on the ground."
    }

    const val BLOCKED_BY_LAND_ALL_WAITING =
        "A LAND ALL sent to the real ground station is still waiting for its answer. Demo mode starts once the " +
            "Ground tab shows the answer."
    const val BLOCKED_BY_LAND_ALL_FAILED =
        "The last LAND ALL to the real ground station failed or did not reach every drone. Make sure every drone " +
            "has landed and dismiss the result on the Ground tab before you start demo mode."
    const val BLOCKED_BY_LAND_WAITING =
        "A Land sent to a real drone is still waiting for its answer. Demo mode starts once the Ground tab shows it."
    /** The same words as the iOS app. */
    const val BLOCKED_BY_FLEET_UNKNOWN =
        "The ground has not reported its drones yet. Wait for it, or switch the link off in Settings first."
    const val BLOCKED_BY_FAILURE =
        "The Ground tab shows a failure from the real ground station (a failed command, a drone alert or a failure " +
            "banner). Check it and dismiss it on the Ground tab before you start demo mode."

    /** The Ground screen's link line while the demo runs. */
    const val GROUND_LINK = "Demo ground: a recorded mission on this phone. No real drones; nothing is sent."
    const val GROUND_PILL = "DEMO"
    const val CONNECTION_NOTE =
        "Demo mode is on and the ground link is off. These settings are kept for your real ground station; " +
            "leave demo mode in Settings, then turn the link on to reconnect."
    const val TEST_IN_DEMO = "Leave demo mode in Settings to test the real ground station."
    const val LINK_ON_IN_DEMO = "Leave demo mode in Settings before you turn the ground link on."

    /** Shown on the Ground tab after the demo, which left the real link off. */
    fun ended(host: String?): String =
        if (host != null) {
            "Demo ended. The ground link is off; turn it on to reconnect to $host."
        } else {
            "Demo ended. The ground link is off."
        }

    // The demo ground's own messages. The Ground screen adds [PREFIX] to them as to every demo result.
    const val LAND_ALL_ACKNOWLEDGED =
        "LAND ALL was answered on this phone: only the simulated drones landed. Nothing was sent to any drone or ground station."
    const val PROFILE_ACKNOWLEDGED = "The profile choice was answered on this phone. Nothing was sent."

    fun landAcknowledged(droneId: String): String =
        "Land for ${GroundText.shortId(droneId)} was answered on this phone: only the simulated drone landed. " +
            "Nothing was sent to any drone or ground station."

    fun resumeAcknowledged(droneId: String): String =
        "Resume search for ${GroundText.shortId(droneId)} was answered on this phone. Nothing was sent."

    /** A LAND ALL (demo) tap whose demo ended before it went out. */
    const val LAND_ALL_DEMO_ENDED =
        "LAND ALL (demo) was not sent: demo mode ended after the tap. Nothing was sent to any ground station."

    // Target tab
    const val TARGET_PLACEHOLDER = "Leave demo mode to choose a photo."
    const val TARGET_PLACEHOLDER_DETAIL =
        "Drawn placeholder target. No photo needed."

    // Search tab
    const val SEARCH_NOTE = "Demo search: a drawn scene. Nothing is sent."
    const val DRONE_NOT_SENT = "Demo mode: no real drone. The command was acknowledged on this phone and nothing was sent."
}
