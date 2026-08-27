"""Multi-feature confidence scorer.

Combines ReID body similarity, face recognition, and LLM reasoning
into a single weighted confidence score. This reduces false positives
by requiring agreement across multiple signals.

New signals (thermal, gait, etc.) can be added without touching this
module's internals via `register_signal()` — see MatchScorer.score().
"""

from amber.config import get_config

# Names populated internally via score()'s named params (reid_score,
# face_score, reasoning_result). These may not be used as **signals
# keys or register_signal() names — doing so would silently shadow the
# named-param value with whatever was passed through **signals.
BUILTIN_SIGNALS = {"reid", "face", "reasoning"}


class MatchScorer:
    """Weighted multi-signal match scorer.

    Thread-safety note: `register_signal()` mutates `_signals` without
    locking. Call it only during setup (e.g. right after construction,
    before the scorer is shared across threads/tasks) — not from
    concurrent code paths that might also be calling `score()`.
    """

    def __init__(
        self,
        reid_weight: float | None = None,
        face_weight: float | None = None,
        reasoning_weight: float | None = None,
        match_threshold: float | None = None,
    ):
        """Initialize the scorer.

        Args:
            reid_weight: Weight for full-body ReID similarity.
                         Defaults to `config.vision.scorer_reid_weight`.
            face_weight: Weight for face recognition score.
                         Defaults to `config.vision.scorer_face_weight`.
            reasoning_weight: Weight for LLM reasoning confidence.
                         Defaults to `config.vision.scorer_reasoning_weight`.
            match_threshold: Combined score threshold for a positive match.
                         Defaults to `config.vision.scorer_match_threshold`.
        """
        cfg = get_config().vision
        self.reid_weight = reid_weight if reid_weight is not None else cfg.scorer_reid_weight
        self.face_weight = face_weight if face_weight is not None else cfg.scorer_face_weight
        self.reasoning_weight = (
            reasoning_weight if reasoning_weight is not None else cfg.scorer_reasoning_weight
        )
        self.match_threshold = (
            match_threshold if match_threshold is not None else cfg.scorer_match_threshold
        )

        # Signal registry: name -> config (currently just weight). The
        # three built-in signals are seeded here so they flow through the
        # exact same weighting/redistribution logic as anything registered
        # later via register_signal() — no special-cased math for "new"
        # signals vs. the original three.
        self._signals: dict[str, dict] = {
            "reid": {"weight": self.reid_weight},
            "face": {"weight": self.face_weight},
            "reasoning": {"weight": self.reasoning_weight},
        }

    def register_signal(self, name: str, weight: float) -> None:
        """Register a new named signal (and its weight) for use in score().

        Once registered, pass the signal's numeric score (0-1) as a keyword
        argument named `name` to score(), e.g.:

            scorer.register_signal("thermal", weight=0.15)
            scorer.score(reid_score=0.8, thermal=0.6)

        Registering a name that already exists overwrites its weight.

        Not thread-safe: this mutates the internal signal registry without
        a lock. Only call it during initialization, before the scorer is
        shared across concurrent code paths.

        Raises:
            ValueError: If `name` collides with a built-in signal name
                ("reid", "face", "reasoning") — those are populated via
                score()'s named parameters and cannot be repointed here.
        """
        if name in BUILTIN_SIGNALS:
            raise ValueError(f"Cannot override built-in signal '{name}'")
        self._signals[name] = {"weight": weight}

    def score(
        self,
        reid_score: float = 0.0,
        face_score: float = 0.0,
        reasoning_result: dict | None = None,
        **signals: float,
    ) -> dict:
        """Compute a combined match score.

        Args:
            reid_score: Cosine similarity from ReID (0-1).
            face_score: Cosine similarity from face recognition (0-1).
            reasoning_result: Dict from AmberAgent with 'match', 'confidence' keys.
            **signals: Additional signals registered via register_signal(),
                passed as `name=score` keyword arguments (0-1 each).

        Returns:
            Dict with:
                combined_score: Weighted score (0-1)
                is_match: Whether combined score exceeds threshold
                breakdown: Individual scores and their weighted contributions
                confidence_level: "high", "medium", or "low"
        """
        # Convert reasoning to a numeric score
        reasoning_score = self._reasoning_to_score(reasoning_result)

        # Assemble raw scores for every signal supplied this call. Order
        # matters only for breakdown's key order, which mirrors the
        # original reid -> face -> reasoning -> extras layout.
        raw_scores: dict[str, float] = {
            "reid": reid_score,
            "face": face_score,
        }

        # reasoning is excluded unless the LLM actually reported a match —
        # a confident "no match" shouldn't be treated as a positive signal.
        if reasoning_score > 0 and (reasoning_result is None or reasoning_result.get("match", False)):
            raw_scores["reasoning"] = reasoning_score

        for name, value in signals.items():
            if name in BUILTIN_SIGNALS:
                raise ValueError(
                    f"Cannot override built-in signal '{name}' via **signals; "
                    f"use the named parameter instead "
                    f"(reid_score, face_score, or reasoning_result)"
                )
            if name not in self._signals:
                raise ValueError(
                    f"Unknown signal '{name}' — register it first with "
                    f"register_signal({name!r}, weight=...)"
                )
            raw_scores[name] = value

        # Compute active weights (redistribute weight if a signal is missing)
        weights: dict[str, float] = {}
        total_weight = 0.0
        for name, value in raw_scores.items():
            if value > 0:
                w = self._signals[name]["weight"]
                weights[name] = w
                total_weight += w

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
        for name, w in norm_weights.items():
            contribution = raw_scores[name] * w
            combined += contribution
            breakdown[name] = {
                "raw_score": round(raw_scores[name], 3),
                "weight": round(w, 2),
                "contribution": round(contribution, 3),
            }

        combined = round(combined, 3)

        # Determine confidence level
        num_signals = len(weights)
        if combined >= 0.65 and num_signals >= 2:
            confidence_level = "high"
        elif combined >= 0.40 or (combined >= 0.35 and num_signals >= 2):
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

        if score >= 0.65 and signals >= 2 and conf == "high":
            return "confirmed_match"
        elif score >= self.match_threshold and conf in ("medium", "high"):
            return "possible_match"
        elif score >= self.match_threshold * 0.5:
            return "weak_signal"
        return "no_match"

    def _reasoning_to_score(self, result: dict | None) -> float:
        """Convert LLM reasoning result to a numeric score.

        A "no match" returns a neutral 0.3 rather than 0.0 to avoid
        vetoing reliable signals (face embeddings, ReID) when the LLM
        misjudges low-quality drone footage.
        """
        if result is None:
            return 0.0

        confidence = result.get("confidence", "unknown").lower()
        if result.get("match", False):
            scores = {"high": 0.90, "medium": 0.65, "low": 0.40}
            return scores.get(confidence, 0.50)
        # No match — return a dampened score, not zero
        scores = {"high": 0.10, "medium": 0.20, "low": 0.30}
        return scores.get(confidence, 0.30)
