package com.flightrisk.app.persistence

import androidx.sqlite.db.SupportSQLiteDatabase
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.lang.reflect.Proxy
import java.sql.Connection
import java.sql.DriverManager

/**
 * Checks the Room schema history of [FlightRiskDatabase] on the JVM.
 *
 * Room's runtime does not run on the JVM (there is no Robolectric or
 * room-testing in this build), so the migration is run here against the
 * desktop SQLite engine bundled with sqlite-jdbc, not Android's SQLite and
 * not through Room. The schema is taken from the files Room exports at
 * compile time. `FlightRiskMigrationInstrumentedTest` runs the same migration
 * through Room on a device or emulator.
 */
class FlightRiskMigrationTest {

    /** Unit tests run with the module directory (mobile/app) as working directory. */
    private val schemaDir = File("schemas/com.flightrisk.app.persistence.FlightRiskDatabase")

    private fun schema(version: Int): JsonObject {
        val file = File(schemaDir, "$version.json")
        check(file.isFile) { "missing ${file.absolutePath}" }
        return Json.parseToJsonElement(file.readText()).jsonObject.getValue("database").jsonObject
    }

    private fun tables(database: JsonObject): Map<String, JsonObject> =
        database.getValue("entities").jsonArray
            .map { it.jsonObject }
            .associateBy { it.getValue("tableName").jsonPrimitive.content }

    @Test
    fun `the migrations form one chain from version 1 to the current version`() {
        var version = 1
        for (migration in FlightRiskMigrations.ALL) {
            assertEquals("a migration starts at the wrong version", version, migration.startVersion)
            assertEquals("a migration skips a version", version + 1, migration.endVersion)
            version = migration.endVersion
        }
        assertEquals(FlightRiskDatabase.SCHEMA_VERSION, version)
    }

    @Test
    fun `a schema file is exported for every version and none beyond the current one`() {
        for (version in 1..FlightRiskDatabase.SCHEMA_VERSION) {
            assertEquals(version, schema(version).getValue("version").jsonPrimitive.int)
        }
        assertFalse(File(schemaDir, "${FlightRiskDatabase.SCHEMA_VERSION + 1}.json").exists())
    }

    @Test
    fun `version 2 drops target_versions and keeps every other table exactly as it was`() {
        val v1 = tables(schema(1))
        val v2 = tables(schema(2))
        assertEquals(setOf("sessions", "matches", "match_feedback", "target_versions"), v1.keys)
        assertEquals(setOf("sessions", "matches", "match_feedback"), v2.keys)
        // Columns, keys, foreign keys and indexes are unchanged, so the
        // migration needs to drop the one table and touch nothing else.
        for (name in v2.keys) assertEquals(name, v1.getValue(name), v2.getValue(name))
    }

    @Test
    fun `migration 1 to 2 runs exactly one statement, the drop of target_versions`() {
        val statements = mutableListOf<String>()
        FlightRiskMigrations.MIGRATION_1_2.migrate(recordingDatabase(statements) {})
        assertEquals(listOf("DROP TABLE IF EXISTS `target_versions`"), statements)
    }

    @Test
    fun `migration 1 to 2 on SQLite keeps the saved sessions, matches and feedback`() {
        DriverManager.getConnection("jdbc:sqlite::memory:").use { conn ->
            exec(conn, "PRAGMA foreign_keys = ON")
            createSchema(conn, schema(1))
            exec(
                conn,
                "INSERT INTO sessions (id, startedAt, endedAt, source, targetDescription, totalFrames, " +
                    "totalDetections, totalMatches, recordingPath) VALUES ('s1', '2026-11-01T10:00:00', " +
                    "'2026-11-01T10:05:00', 'camera', 'red jacket', 300, 12, 1, NULL)",
            )
            exec(
                conn,
                "INSERT INTO matches (id, sessionId, timestamp, matchType, reidScore, faceScore, combinedScore, " +
                    "gemmaMatch, gemmaConfidence, reasoning, snapshotPath, cropPath) VALUES (7, 's1', " +
                    "'2026-11-01T10:01:00', 'possible_match', 0.61, 0.0, 0.61, 0, NULL, NULL, NULL, NULL)",
            )
            exec(
                conn,
                "INSERT INTO match_feedback (matchId, sessionId, feedback, timestamp, notes) " +
                    "VALUES (7, 's1', 'confirmed', '2026-11-01T10:02:00', NULL)",
            )
            exec(conn, "INSERT INTO target_versions (timestamp, operatorId, imageB64, isActive) VALUES (1, 'default', 'AAAA', 1)")

            FlightRiskMigrations.MIGRATION_1_2.migrate(recordingDatabase(mutableListOf()) { exec(conn, it) })

            assertEquals(listOf("s1|red jacket|300|12|1"), rows(conn, "SELECT id, targetDescription, totalFrames, totalDetections, totalMatches FROM sessions"))
            assertEquals(listOf("7|s1|possible_match|0.61"), rows(conn, "SELECT id, sessionId, matchType, combinedScore FROM matches"))
            assertEquals(listOf("7|s1|confirmed"), rows(conn, "SELECT matchId, sessionId, feedback FROM match_feedback"))
            assertEquals(emptyList<String>(), rows(conn, "PRAGMA foreign_key_check"))
            // What is left is exactly the version 2 schema, table by table and index by index.
            assertEquals(expectedObjects(schema(2)), actualObjects(conn))
        }
    }

    @Test
    fun `the database registers every migration and has no destructive fallback`() {
        val src = File("src/main/java/com/flightrisk/app/persistence/FlightRiskDatabase.kt").readText()
        assertFalse(src.contains("fallbackToDestructive"))
        assertTrue(src.contains(".addMigrations(*FlightRiskMigrations.ALL)"))
        assertTrue(src.contains("version = FlightRiskDatabase.SCHEMA_VERSION"))
        assertTrue(src.contains("exportSchema = true"))
        val gradle = File("build.gradle.kts").readText()
        assertTrue(gradle.contains("arg(\"room.schemaLocation\", \"\$projectDir/schemas\")"))
    }

    @Test
    fun `no app code still uses the removed target_versions table`() {
        val offenders = File("src/main/java").walk()
            .filter { it.isFile && it.extension == "kt" && it.name != "FlightRiskMigrations.kt" }
            .filter { f -> f.readText().let { it.contains("target_versions") || it.contains("TargetVersion") || it.contains("TargetCanon") } }
            .map { it.path }
            .toList()
        assertEquals(emptyList<String>(), offenders)
    }

    // -- helpers --

    private fun createSchema(conn: Connection, database: JsonObject) {
        for (entity in tables(database).values) {
            val table = entity.getValue("tableName").jsonPrimitive.content
            exec(conn, entity.getValue("createSql").jsonPrimitive.content.replace("\${TABLE_NAME}", table))
            entity["indices"]?.jsonArray?.forEach {
                exec(conn, it.jsonObject.getValue("createSql").jsonPrimitive.content.replace("\${TABLE_NAME}", table))
            }
        }
        database.getValue("setupQueries").jsonArray.forEach { exec(conn, it.jsonPrimitive.content) }
    }

    /** The tables and indexes the schema file declares, as "type name: sql". */
    private fun expectedObjects(database: JsonObject): Set<String> =
        tables(database).values.flatMap { entity ->
            val table = entity.getValue("tableName").jsonPrimitive.content
            val own = listOf("table $table: " + normalize(entity.getValue("createSql").jsonPrimitive.content.replace("\${TABLE_NAME}", table)))
            val indices = entity["indices"]?.jsonArray.orEmpty().map {
                val index = it.jsonObject
                "index ${index.getValue("name").jsonPrimitive.content}: " +
                    normalize(index.getValue("createSql").jsonPrimitive.content.replace("\${TABLE_NAME}", table))
            }
            own + indices
        }.toSet()

    /** The tables and indexes in the database, without SQLite's and Room's own. */
    private fun actualObjects(conn: Connection): Set<String> =
        rows(
            conn,
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' " +
                "AND name <> 'room_master_table' AND sql IS NOT NULL",
        ).map { row ->
            val (type, name, sql) = row.split('|', limit = 3)
            "$type $name: ${normalize(sql)}"
        }.toSet()

    /** SQLite stores CREATE statements without IF NOT EXISTS. */
    private fun normalize(sql: String): String =
        sql.replace("IF NOT EXISTS ", "").replace(Regex("\\s+"), " ").trim()

    private fun exec(conn: Connection, sql: String) {
        conn.createStatement().use { it.execute(sql) }
    }

    private fun rows(conn: Connection, sql: String): List<String> =
        conn.createStatement().use { st ->
            st.executeQuery(sql).use { rs ->
                val columns = rs.metaData.columnCount
                buildList {
                    while (rs.next()) add((1..columns).joinToString("|") { rs.getString(it) ?: "null" })
                }
            }
        }

    /**
     * A [SupportSQLiteDatabase] that records each `execSQL(String)` and hands
     * it to [run]. Any other call fails the test, so a migration that needs
     * more than plain statements is noticed here.
     */
    private fun recordingDatabase(statements: MutableList<String>, run: (String) -> Unit): SupportSQLiteDatabase =
        Proxy.newProxyInstance(
            SupportSQLiteDatabase::class.java.classLoader,
            arrayOf(SupportSQLiteDatabase::class.java),
        ) { proxy, method, args ->
            when {
                method.name == "execSQL" && args?.size == 1 -> {
                    val sql = args[0] as String
                    statements += sql
                    run(sql)
                    null
                }
                method.name == "toString" -> "RecordingDatabase"
                method.name == "hashCode" -> System.identityHashCode(proxy)
                method.name == "equals" -> proxy === args?.get(0)
                else -> throw UnsupportedOperationException("the migration called ${method.name}")
            }
        } as SupportSQLiteDatabase
}
