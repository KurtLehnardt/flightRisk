package com.flightrisk.app.privacy

import android.content.Context

/**
 * Local-only privacy choices of this install: whether the privacy notice has
 * been acknowledged and whether search video is recorded.
 *
 * Both values describe a decision made on this phone, so they are kept in a
 * file that backup and device transfer exclude (see backup_rules.xml and
 * data_extraction_rules.xml). A restored phone therefore shows the notice
 * again and does not record video until the person turns that on.
 */
interface PrivacyPrefs {
    /** True once the person tapped "I Understand" on the current notice. */
    var noticeAcknowledged: Boolean

    /** True only if the person turned on "Record search video". Off by default. */
    var recordSearchVideo: Boolean
}

/** [PrivacyPrefs] over the excluded SharedPreferences file [FILE]. */
class SharedPrefsPrivacyPrefs(context: Context) : PrivacyPrefs {
    private val prefs = context.applicationContext.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    override var noticeAcknowledged: Boolean
        get() = prefs.getBoolean(KEY_NOTICE, false)
        set(value) {
            prefs.edit().putBoolean(KEY_NOTICE, value).apply()
        }

    override var recordSearchVideo: Boolean
        get() = prefs.getBoolean(KEY_RECORD, false)
        set(value) {
            prefs.edit().putBoolean(KEY_RECORD, value).apply()
        }

    companion object {
        /** The file name without ".xml"; the backup rules exclude "$FILE.xml". */
        const val FILE = "flightrisk_privacy_local"
        const val KEY_NOTICE = "privacy_notice_acknowledged_v2"
        const val KEY_RECORD = "record_search_video"
    }
}
