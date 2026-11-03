package com.flightrisk.app.persistence

import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

/**
 * Every schema migration of [FlightRiskDatabase], in version order.
 *
 * The database has no destructive fallback, so a version bump must add a
 * migration here and export the new schema file under `app/schemas`.
 * Without one, Room refuses to open the database (and the saved sessions
 * stay on disk) instead of erasing it; `FlightRiskMigrationTest` fails first.
 */
object FlightRiskMigrations {

    /**
     * The only statement of the version 1 to 2 migration. Version 1 had a
     * `target_versions` table for the target canon code, which nothing used
     * and which was removed. The other tables are identical in both versions
     * (checked against the exported schema files), so dropping this one table
     * is the whole migration.
     */
    const val DROP_TARGET_VERSIONS_SQL = "DROP TABLE IF EXISTS `target_versions`"

    val MIGRATION_1_2: Migration = object : Migration(1, 2) {
        override fun migrate(db: SupportSQLiteDatabase) {
            db.execSQL(DROP_TARGET_VERSIONS_SQL)
        }
    }

    /** All migrations, passed to the database builder. */
    val ALL: Array<Migration> = arrayOf(MIGRATION_1_2)
}
