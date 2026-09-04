"""Threshold learning from operator feedback.

Analyzes confirmed and rejected match feedback to suggest an
optimal match threshold that separates true positives from false positives.
"""

import sqlite3
import statistics


class ThresholdTuner:
    """Learns optimal match threshold from operator feedback history."""

    def __init__(self, db):
        self.db = db

    def analyze(self, current_threshold: float = 0.50) -> dict:
        """Analyze feedback and suggest an optimal threshold.

        Queries confirmed and rejected matches from the match_feedback table,
        computes score distributions, and suggests a midpoint threshold that
        best separates the two groups.

        Args:
            current_threshold: The currently active match threshold.

        Returns:
            Dict with: current_threshold, suggested_threshold, confidence,
                       reason, stats
        """
        try:
            confirmed_rows = self.db._conn.execute(
                """SELECT m.combined_score, m.reid_score, m.face_score
                   FROM matches m JOIN match_feedback f ON m.id = f.match_id
                   WHERE f.feedback = 'confirmed' AND m.combined_score > 0"""
            ).fetchall()

            rejected_rows = self.db._conn.execute(
                """SELECT m.combined_score, m.reid_score, m.face_score
                   FROM matches m JOIN match_feedback f ON m.id = f.match_id
                   WHERE f.feedback = 'rejected' AND m.combined_score > 0"""
            ).fetchall()
        except sqlite3.OperationalError:
            # match_feedback table doesn't exist yet
            return {
                "current_threshold": current_threshold,
                "suggested_threshold": current_threshold,
                "confidence": "insufficient_data",
                "reason": "Feedback table not yet available.",
                "stats": {},
            }

        confirmed_scores = [row[0] for row in confirmed_rows]
        rejected_scores = [row[0] for row in rejected_rows]

        # Need at least 5 of each for a meaningful analysis
        if len(confirmed_scores) < 5 or len(rejected_scores) < 5:
            return {
                "current_threshold": current_threshold,
                "suggested_threshold": current_threshold,
                "confidence": "insufficient_data",
                "reason": (
                    f"Need at least 5 confirmed and 5 rejected matches. "
                    f"Have {len(confirmed_scores)} confirmed, {len(rejected_scores)} rejected."
                ),
                "stats": {
                    "confirmed_count": len(confirmed_scores),
                    "rejected_count": len(rejected_scores),
                },
            }

        # Compute statistics for each group
        confirmed_stats = {
            "count": len(confirmed_scores),
            "avg": round(statistics.mean(confirmed_scores), 4),
            "min": round(min(confirmed_scores), 4),
            "max": round(max(confirmed_scores), 4),
            "stdev": round(statistics.stdev(confirmed_scores), 4) if len(confirmed_scores) > 1 else 0,
        }
        rejected_stats = {
            "count": len(rejected_scores),
            "avg": round(statistics.mean(rejected_scores), 4),
            "min": round(min(rejected_scores), 4),
            "max": round(max(rejected_scores), 4),
            "stdev": round(statistics.stdev(rejected_scores), 4) if len(rejected_scores) > 1 else 0,
        }

        # Compute percentiles for threshold suggestion
        confirmed_sorted = sorted(confirmed_scores)
        rejected_sorted = sorted(rejected_scores)

        # 10th percentile of confirmed (low end of true positives)
        p10_idx = max(0, int(len(confirmed_sorted) * 0.10))
        confirmed_p10 = confirmed_sorted[p10_idx]

        # 90th percentile of rejected (high end of false positives)
        p90_idx = min(len(rejected_sorted) - 1, int(len(rejected_sorted) * 0.90))
        rejected_p90 = rejected_sorted[p90_idx]

        # Suggested threshold is the midpoint
        suggested = (confirmed_p10 + rejected_p90) / 2.0

        # Clamp to safe range
        suggested = max(0.20, min(0.90, suggested))
        suggested = round(suggested, 3)

        # Determine confidence based on separation
        if min(confirmed_scores) > max(rejected_scores):
            confidence = "high"
            reason = (
                f"Clean separation: lowest confirmed ({min(confirmed_scores):.3f}) > "
                f"highest rejected ({max(rejected_scores):.3f})."
            )
        elif confirmed_p10 > rejected_p90:
            confidence = "medium"
            reason = (
                f"Good separation at percentile boundaries. "
                f"Confirmed 10th pct ({confirmed_p10:.3f}) > rejected 90th pct ({rejected_p90:.3f})."
            )
        else:
            confidence = "low"
            reason = (
                f"Score distributions overlap. "
                f"Confirmed 10th pct ({confirmed_p10:.3f}) <= rejected 90th pct ({rejected_p90:.3f}). "
                f"Threshold may produce errors in either direction."
            )

        return {
            "current_threshold": current_threshold,
            "suggested_threshold": suggested,
            "confidence": confidence,
            "reason": reason,
            "stats": {
                "confirmed": confirmed_stats,
                "rejected": rejected_stats,
            },
        }
