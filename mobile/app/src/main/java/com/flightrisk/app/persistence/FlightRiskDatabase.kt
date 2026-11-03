package com.flightrisk.app.persistence

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

@Database(
    entities = [
        SessionEntity::class,
        MatchEntity::class,
        MatchFeedbackEntity::class,
    ],
    version = FlightRiskDatabase.SCHEMA_VERSION,
    exportSchema = true,
)
abstract class FlightRiskDatabase : RoomDatabase() {

    abstract fun sessionDao(): SessionDao

    companion object {
        /**
         * The schema version. Raising it needs a migration in
         * [FlightRiskMigrations] and the exported schema file for the new
         * version; `FlightRiskMigrationTest` checks both.
         */
        const val SCHEMA_VERSION = 2

        const val DATABASE_NAME = "flightrisk_sessions.db"

        @Volatile
        private var instance: FlightRiskDatabase? = null

        fun getInstance(context: Context): FlightRiskDatabase {
            return instance ?: synchronized(this) {
                instance ?: builder(context, DATABASE_NAME)
                    .build()
                    .also { instance = it }
            }
        }

        /**
         * The builder the app uses, also used by the instrumented migration
         * test. There is deliberately no destructive fallback, not even for a
         * downgrade: when no migration path exists, Room fails to open the
         * database and the saved sessions stay on disk. The callers already
         * handle that failure (the search runs without saving a session, and
         * the delete action reports an error), whereas a fallback would erase
         * the sessions without a word.
         */
        internal fun builder(context: Context, name: String): RoomDatabase.Builder<FlightRiskDatabase> =
            Room.databaseBuilder(context.applicationContext, FlightRiskDatabase::class.java, name)
                .addMigrations(*FlightRiskMigrations.ALL)
    }
}
