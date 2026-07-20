"""Multi-feature confidence scorer.

Combines ReID body similarity, face recognition, and LLM reasoning
into a single weighted confidence score. This reduces false positives
by requiring agreement across multiple signals.
"""


class MatchScorer:
    """Weighted multi-signal match scorer."""

    def __init__(
        self,
        reid_weight: float = 0.35,
        face_weight: float = 0.40,
        reasoning_weight: float = 0.25,
        match_threshold: float = 0.50,
    ):
        """Initialize the scorer.

        Args:
            reid_weight: Weight for full-body ReID similarity.
            face_weight: Weight for face recognition score.
            reasoning_weight: Weight for LLM reasoning confidence.
            match_threshold: Combined score threshold for a positive match.
        """
        self.reid_weight = reid_weight
        self.face_weight = face_weight
        self.reasoning_weight = reasoning_weight
        self.match_threshold = match_threshold

    def score(
        self,
        reid_score: float = 0.0,
        face_score: float = 0.0,
        reasoning_result: dict | None = None,
    ) -> dict:
        """Compute a combined match score.

        Args:
            reid_score: Cosine similarity from ReID (0-1).
            face_score: Cosine similarity from face recognition (0-1).
            reasoning_result: Dict from AmberAgent with 'match', 'confidence' keys.

        Returns:
            Dict with:
                combined_score: Weighted score (0-1)
                is_match: Whether combined score exceeds threshold
                breakdown: Individual scores and their weighted contributions
                confidence_level: "high", "medium", or "low"
        """
        # Convert reasoning to a numeric score
        reasoning_score = self._reasoning_to_score(reasoning_result)

        # Compute active weights (redistribute weight if a signal is missing)
        weights = {}
        total_weight = 0.0

        if reid_score > 0:
            weights["reid"] = self.reid_weight
            total_weight += self.reid_weight

        if face_score > 0:
            weights["face"] = self.face_weight
            total_weight += self.face_weight

        if reasoning_score > 0:
            weights["reasoning"] = self.reasoning_weight
            total_weight += self.reasoning_weight

        # If no signals, return zero
        if total_weight == 0:
            return {
                "combined_score": 0.0,
                "is_match": False,
                "breakdown": {},
                "confidence_level": "none",
            }

        # Normalize weights to sum to 1.0 (redistribute missing signal weight)
        norm_weights = {k: v / total_weight for k, v in weights.items()}

        # Weighted sum
        combined = 0.0
        breakdown = {}

        if "reid" in norm_weights:
            contribution = reid_score * norm_weights["reid"]
            combined += contribution
            breakdown["reid"] = {
                "raw_score": round(reid_score, 3),
                "weight": round(norm_weights["reid"], 2),
                "contribution": round(contribution, 3),
            }

        if "face" in norm_weights:
            contribution = face_score * norm_weights["face"]
            combined += contribution
            breakdown["face"] = {
                "raw_score": round(face_score, 3),
                "weight": round(norm_weights["face"], 2),
                "contribution": round(contribution, 3),
            }

        if "reasoning" in norm_weights:
            contribution = reasoning_score * norm_weights["reasoning"]
            combined += contribution
            breakdown["reasoning"] = {
                "raw_score": round(reasoning_score, 3),
                "weight": round(norm_weights["reasoning"], 2),
                "contribution": round(contribution, 3),
            }

        combined = round(combined, 3)

        # Determine confidence level
        num_signals = len(weights)
        if combined >= 0.75 and num_signals >= 2:
            confidence_level = "high"
        elif combined >= 0.55 or (combined >= 0.45 and num_signals >= 2):
            confidence_level = "medium"
        else:
            confidence_level = "low"

        return {
            "combined_score": combined,
            "is_match": combined >= self.match_threshold,
            "breakdown": breakdown,
            "confidence_level": confidence_level,
            "signals_used": num_signals,
        }

    def alert_level(self, score_result: dict) -> str:
        """Determine alert level from score result.

        Returns: 'confirmed_match', 'possible_match', 'weak_signal', or 'no_match'
        """
        score = score_result.get("combined_score", 0)
        signals = score_result.get("signals_used", 0)
        conf = score_result.get("confidence_level", "low")

        if score >= 0.70 and signals >= 2 and conf == "high":
            return "confirmed_match"
        elif score >= self.match_threshold and conf in ("medium", "high"):
            return "possible_match"
        elif score >= self.match_threshold * 0.7:
            return "weak_signal"
        return "no_match"

    def _reasoning_to_score(self, result: dict | None) -> float:
        """Convert LLM reasoning result to a numeric score."""
        if result is None:
            return 0.0

        if not result.get("match", False):
            return 0.0

        confidence = result.get("confidence", "unknown").lower()
        scores = {
            "high": 0.90,
            "medium": 0.65,
            "low": 0.40,
        }
        return scores.get(confidence, 0.50)
