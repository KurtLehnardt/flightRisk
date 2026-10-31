package com.flightrisk.app.privacy

/**
 * The words of the first-launch privacy notice and of the recording setting.
 * They are kept here, apart from the screens, so a unit test can check that
 * they are not stale. The facts match the iOS notice and the privacy policy
 * (docs/privacy-policy.md).
 */
object PrivacyNoticeText {
    const val TITLE = "Privacy Notice"

    const val POLICY_URL = "https://github.com/KurtLehnardt/flightRisk/blob/main/docs/privacy-policy.md"
    const val POLICY_LINK_LABEL = "Read the full privacy policy"

    data class Item(val title: String, val description: String)

    val items: List<Item> = listOf(
        Item(
            "Camera Access",
            "FlightRisk uses your camera to scan for people who match the target photo you provide.",
        ),
        Item(
            "On-Device Processing",
            "Person detection and matching run on your device. Camera frames are not uploaded. " +
                "Search video is not recorded unless you turn on Record search video in Settings. " +
                "Cloud reasoning, a ground station and a Tello drone are described below.",
        ),
        Item(
            "Ground Station (Optional)",
            "If you add a ground station address in Settings, the app connects to that address only, " +
                "to show drone status and send land, resume-search and detection-profile commands. " +
                "A ws:// or http:// link is not encrypted, so use a network you trust.",
        ),
        Item(
            "Phone as an Edge (Off by Default)",
            "If you turn on the ground station link in Settings, then while a search is running " +
                "this phone sends what its camera detects to the address you entered: box positions " +
                "and scores, not pictures of people and not face data. If the ground station asks for " +
                "video, the phone also sends a small picture of the whole camera view, which can show people. " +
                "You can turn it off at any time.",
        ),
        Item(
            "Tello Drone (Optional)",
            "If you connect a Tello drone, the app joins the drone's own Wi-Fi network and receives its " +
                "video and telemetry on your device. That link is not encrypted. Frames from the drone " +
                "are processed on your device in the same way as camera frames.",
        ),
        Item(
            "Cloud Reasoning (Off by Default)",
            "Cloud reasoning is off. If you turn it on in Settings, save a Claude API key and select " +
                "Cloud Claude, the cropped image of each detected person, the reference photo and the " +
                "text description you entered are sent to Anthropic. The images can show a face. " +
                "You can turn it off at any time.",
        ),
        Item(
            "Location Data",
            "GPS coordinates are attached to match alerts so you can navigate to the location. " +
                "Location data stays on your device.",
        ),
        Item(
            "No Analytics or Accounts",
            "FlightRisk does not collect analytics or telemetry. There is no account or sign-in.",
        ),
    )

    /** Every sentence of the notice, for tests. */
    fun allText(): String = items.joinToString("\n") { it.title + ". " + it.description }
}

/** The words of the "Record search video" setting and of the delete action. */
object PrivacySettingsText {
    const val RECORD_TITLE = "Record search video"

    const val RECORD_EXPLANATION =
        "When this is on, each search is saved as a video file in this app's private storage on " +
            "this phone, and match pictures are saved with it. The video can show people, including " +
            "faces. Phone backup does not include these files, and you can delete them with " +
            "\"Delete saved sessions and recordings\" below. It is off by default."

    const val DELETE_TITLE = "Delete saved sessions and recordings"
    const val DELETE_CONFIRM_TITLE = "Delete all saved sessions?"
    const val DELETE_CONFIRM_BODY =
        "This permanently deletes the saved search sessions, matches and feedback on this phone, " +
            "and all recorded videos and match pictures. It cannot be undone."
    const val DELETE_CONFIRM_BUTTON = "Delete"
    const val DELETE_CANCEL_BUTTON = "Cancel"
    const val DELETE_BLOCKED_WHILE_SEARCHING = "Stop the search before deleting."

    fun deleteResult(result: SavedDataEraser.Result): String =
        if (result.failedFiles > 0) {
            "Deleted ${result.rows} saved records and ${result.files} files. " +
                "${result.failedFiles} files could not be deleted."
        } else {
            "Deleted ${result.rows} saved records and ${result.files} files."
        }

    const val DELETE_FAILED = "Could not delete the saved sessions."
}
