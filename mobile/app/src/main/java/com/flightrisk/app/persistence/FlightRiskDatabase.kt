package com.flightrisk.app.persistence

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import com.flightrisk.app.canon.TargetVersionDao
import com.flightrisk.app.canon.TargetVersionEntity

@Database(
    entities = [
        SessionEntity::class,
        MatchEntity::class,
        MatchFeedbackEntity::class,
        TargetVersionEntity::class,
    ],
    version = 1,
    exportSchema = false,
)
abstract class FlightRiskDatabase : RoomDatabase() {

    abstract fun sessionDao(): SessionDao
    abstract fun targetVersionDao(): TargetVersionDao

    companion object {
        @Volatile
        private var instance: FlightRiskDatabase? = null

        fun getInstance(context: Context): FlightRiskDatabase {
            return instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    FlightRiskDatabase::class.java,
                    "flightrisk_sessions.db",
                )
                    .fallbackToDestructiveMigration()
                    .build()
                    .also { instance = it }
            }
        }
    }
}
