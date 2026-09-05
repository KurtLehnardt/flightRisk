package com.flightrisk.app.pipeline

import android.graphics.Bitmap

/**
 * Data class representing a match event from the search pipeline.
 *
 * Mirrors the `match_entry` dict emitted in the Python `pipeline.py`
 * and `alerts.py` modules. Every match -- whether from ReID, face
 * recognition, or LLM description matching -- is captured as a
 * [MatchEntry] with scores, alert level, and optional GPS coordinates.
 *
 * @property time Human-readable timestamp (HH:MM:SS).
 * @property score Combined match score from the multi-feature scorer.
 * @property reidScore ReID (re-identification) cosine similarity score.
 * @property faceScore Face recognition similarity score.
 * @property alertLevel One of "confirmed_match", "possible_match",
 *   "weak_signal", or "no_match".
 * @property trackId Spatial track identifier (grid-cell key or tracker ID).
 * @property snapshot Cropped candidate image (may be null if unavailable).
 * @property gemmaMatch Whether LLM reasoning confirmed the match (null if pending).
 * @property gemmaConfidence LLM confidence: "high", "medium", "low", "pending", or null.
 * @property reasoning Free-text reasoning from the LLM, or null if not yet available.
 * @property matchType How the match was detected: "reid", "face", or "description".
 * @property latitude GPS latitude at time of match, or null if unavailable.
 * @property longitude GPS longitude at time of match, or null if unavailable.
 * @property locationAccuracy GPS accuracy in meters, or null if unavailable.
 */
data class MatchEntry(
    val time: String,
    val score: Float,
    val reidScore: Float,
    val faceScore: Float,
    val alertLevel: String,
    val trackId: String,
    val snapshot: Bitmap? = null,
    val gemmaMatch: Boolean? = null,
    val gemmaConfidence: String? = null,
    val reasoning: String? = null,
    val matchType: String = "reid",
    val latitude: Double? = null,
    val longitude: Double? = null,
    val locationAccuracy: Float? = null,
)
