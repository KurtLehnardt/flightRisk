package com.flightrisk.app.privacy

/**
 * Decides what the app may do before the privacy notice has been acknowledged.
 * The camera and location permission prompts, and the onboarding flow, wait
 * until the notice is acknowledged.
 */
object PrivacyNoticeGate {
    fun shouldShowNotice(prefs: PrivacyPrefs): Boolean = !prefs.noticeAcknowledged

    /** Permission prompts are allowed only after the notice was acknowledged. */
    fun mayRequestPermissions(prefs: PrivacyPrefs): Boolean = prefs.noticeAcknowledged

    /** Records the acknowledgement. */
    fun acknowledge(prefs: PrivacyPrefs) {
        prefs.noticeAcknowledged = true
    }
}

/** Where a match is saved; [SessionRepository] is adapted to this by the pipeline. */
interface MatchSink<T : Any> {
    suspend fun addMatch(
        sessionId: String,
        matchType: String,
        reidScore: Double,
        faceScore: Double,
        combinedScore: Double,
        snapshot: T?,
    ): Long
}

/**
 * Saves a match through [sink]. The picture is passed on only while [recording]
 * is on, so with recording off nothing is written to the snapshots folder.
 */
suspend fun <T : Any> persistMatch(
    sink: MatchSink<T>,
    recording: Boolean,
    sessionId: String,
    matchType: String,
    reidScore: Double,
    faceScore: Double,
    combinedScore: Double,
    crop: T?,
): Long = sink.addMatch(
    sessionId, matchType, reidScore, faceScore, combinedScore,
    RecordingPolicy.snapshotToSave(recording, crop),
)

/** What the recorder and the match snapshots do, given the recording setting. */
object RecordingPolicy {
    /** Video is recorded only when the person turned the setting on. */
    fun shouldRecord(prefs: PrivacyPrefs): Boolean = prefs.recordSearchVideo

    /**
     * Match snapshot and crop images are saved only while recording is on, so
     * a person who leaves recording off keeps no image of anyone on the phone.
     */
    fun <T> snapshotToSave(recording: Boolean, image: T?): T? = if (recording) image else null
}
