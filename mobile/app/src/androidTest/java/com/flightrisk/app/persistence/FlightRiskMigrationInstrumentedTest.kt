package com.flightrisk.app.persistence

import android.content.Context
import androidx.sqlite.db.SupportSQLiteDatabase
import androidx.sqlite.db.SupportSQLiteOpenHelper
import androidx.sqlite.db.framework.FrameworkSQLiteOpenHelperFactory
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Runs the real migrations through Room on a device or emulator, starting
 * from a database built from the exported version 1 schema (the schema files
 * are added to the androidTest assets in build.gradle.kts). It uses the app's
 * own [FlightRiskDatabase.builder], so it also covers the missing destructive
 * fallback. room-testing's MigrationTestHelper is not in this build, so the
 * old database is created with the plain SQLite open helper instead.
 */
@RunWith(AndroidJUnit4::class)
class FlightRiskMigrationInstrumentedTest {

    private val name = "migration-test.db"
    private lateinit var context: Context

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        context.deleteDatabase(name)
    }

    @After
    fun tearDown() {
        context.deleteDatabase(name)
    }

    private fun schema(version: Int): JsonObject {
        val assets = InstrumentationRegistry.getInstrumentation().context.assets
        val text = assets.open("com.flightrisk.app.persistence.FlightRiskDatabase/$version.json")
            .bufferedReader().use { it.readText() }
        return Json.parseToJsonElement(text).jsonObject.getValue("database").jsonObject
    }

    /** Creates the database file at [version] with the exported schema, then runs [fill]. */
    private fun createRaw(version: Int, fill: (SupportSQLiteDatabase) -> Unit) {
        val database = schema(version)
        val helper = FrameworkSQLiteOpenHelperFactory().create(
            SupportSQLiteOpenHelper.Configuration.builder(context)
                .name(name)
                .callback(object : SupportSQLiteOpenHelper.Callback(version) {
                    override fun onCreate(db: SupportSQLiteDatabase) {
                        for (entity in database.getValue("entities").jsonArray.map { it.jsonObject }) {
                            val table = entity.getValue("tableName").jsonPrimitive.content
                            db.execSQL(entity.getValue("createSql").jsonPrimitive.content.replace("\${TABLE_NAME}", table))
                            entity["indices"]?.jsonArray?.forEach {
                                db.execSQL(it.jsonObject.getValue("createSql").jsonPrimitive.content.replace("\${TABLE_NAME}", table))
                            }
                        }
                        database.getValue("setupQueries").jsonArray.forEach { db.execSQL(it.jsonPrimitive.content) }
                    }

                    override fun onUpgrade(db: SupportSQLiteDatabase, oldVersion: Int, newVersion: Int) =
                        throw AssertionError("unexpected upgrade")
                })
                .build(),
        )
        try {
            fill(helper.writableDatabase)
        } finally {
            helper.close()
        }
    }

    private fun insertSession(db: SupportSQLiteDatabase) {
        db.execSQL("INSERT INTO sessions (id, startedAt, totalFrames, totalDetections, totalMatches) VALUES ('s1', '2026-11-01T10:00:00', 300, 12, 1)")
        db.execSQL(
            "INSERT INTO matches (id, sessionId, timestamp, matchType, reidScore, faceScore, combinedScore, gemmaMatch) " +
                "VALUES (7, 's1', '2026-11-01T10:01:00', 'possible_match', 0.61, 0.0, 0.61, 0)",
        )
    }

    private fun count(db: SupportSQLiteDatabase, sql: String): Int =
        db.query(sql).use { c -> c.moveToFirst(); c.getInt(0) }

    @Test
    fun version1DatabaseIsMigratedAndKeepsItsSessions() {
        createRaw(1) { db ->
            insertSession(db)
            db.execSQL("INSERT INTO target_versions (timestamp, operatorId, imageB64, isActive) VALUES (1, 'default', 'AAAA', 1)")
        }
        val room = FlightRiskDatabase.builder(context, name).build()
        try {
            val session = runBlocking { room.sessionDao().getSession("s1") }
            assertNotNull("the saved session survived the migration", session)
            assertEquals(300, session!!.totalFrames)
            assertEquals(1, runBlocking { room.sessionDao().getSessionMatches("s1") }.size)
            val db = room.openHelper.readableDatabase
            assertEquals(FlightRiskDatabase.SCHEMA_VERSION, db.version)
            assertEquals(0, count(db, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'target_versions'"))
        } finally {
            room.close()
        }
    }

    @Test
    fun aDatabaseWithNoMigrationPathIsNotErased() {
        // A database from a newer app version (as after a sideloaded downgrade)
        // has no migration path. Without a destructive fallback, Room refuses to
        // open it and the rows stay.
        val newer = FlightRiskDatabase.SCHEMA_VERSION + 1
        createRaw(FlightRiskDatabase.SCHEMA_VERSION) { db ->
            insertSession(db)
            db.version = newer
        }
        val room = FlightRiskDatabase.builder(context, name).build()
        try {
            runBlocking { room.sessionDao().getSessionCount() }
            fail("expected Room to refuse the database")
        } catch (e: IllegalStateException) {
            // Room found no migration from the newer version.
        } finally {
            room.close()
        }
        val helper = FrameworkSQLiteOpenHelperFactory().create(
            SupportSQLiteOpenHelper.Configuration.builder(context)
                .name(name)
                .callback(object : SupportSQLiteOpenHelper.Callback(newer) {
                    override fun onCreate(db: SupportSQLiteDatabase) = throw AssertionError("the file was deleted")
                    override fun onUpgrade(db: SupportSQLiteDatabase, oldVersion: Int, newVersion: Int) =
                        throw AssertionError("unexpected upgrade")
                })
                .build(),
        )
        try {
            assertEquals(1, count(helper.readableDatabase, "SELECT COUNT(*) FROM sessions"))
        } finally {
            helper.close()
        }
    }
}
