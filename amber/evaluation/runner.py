"""Evaluation runner — computes match metrics over labeled datasets.

Loads each image pair, runs through ReID / face / scorer pipeline,
and computes accuracy, precision, recall, F1, and threshold curves.
"""

import cv2
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

from .dataset import EvalPair, EvalDataset


@dataclass
class PairResult:
    """Result of evaluating a single pair."""

    pair: EvalPair
    reid_score: float
    face_score: float
    combined_score: float
    predicted_match: bool


@dataclass
class EvalResult:
    """Aggregate metrics from an evaluation run."""

    accuracy: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0
    false_positive_rate: float = 0.0
    confusion_matrix: dict = field(default_factory=lambda: {"tp": 0, "fp": 0, "tn": 0, "fn": 0})
    per_pair_results: list[PairResult] = field(default_factory=list)
    threshold_curve: list[tuple[float, float, float]] = field(default_factory=list)


class EvalRunner:
    """Runs evaluation pipeline over a labeled dataset."""

    def __init__(self, reid=None, face=None, scorer=None):
        """Initialize with pipeline components.

        Args:
            reid: PersonReID instance (or None to skip body matching).
            face: FaceRecognizer instance (or None to skip face matching).
            scorer: MatchScorer instance (or None — will average available scores).
        """
        self.reid = reid
        self.face = face
        self.scorer = scorer

    def _compute_scores(self, reference: np.ndarray, candidate: np.ndarray) -> tuple[float, float, float]:
        """Compute reid, face, and combined scores for an image pair.

        Returns:
            (reid_score, face_score, combined_score)
        """
        reid_score = 0.0
        face_score = 0.0

        if self.reid is not None:
            self.reid.set_target(reference)
            reid_score = self.reid.compare(candidate)

        if self.face is not None:
            self.face.set_target(reference)
            face_score = self.face.compare(candidate)

        # Compute combined score
        if self.scorer is not None:
            result = self.scorer.score(reid_score=reid_score, face_score=face_score)
            combined_score = result["combined_score"]
        else:
            # Simple average of available scores
            scores = [s for s in [reid_score, face_score] if s > 0]
            combined_score = sum(scores) / len(scores) if scores else 0.0

        return reid_score, face_score, combined_score

    def run(self, dataset: EvalDataset, threshold: float = 0.5) -> EvalResult:
        """Run evaluation over the full dataset.

        Args:
            dataset: Labeled dataset of image pairs.
            threshold: Combined score threshold for predicting a match.

        Returns:
            EvalResult with all metrics.
        """
        pair_results: list[PairResult] = []
        skipped = 0

        for pair in dataset:
            ref_img = cv2.imread(pair.reference_path)
            cand_img = cv2.imread(pair.candidate_path)

            if ref_img is None or cand_img is None:
                print(f"[eval] Skipping pair — cannot load images:")
                print(f"       ref={pair.reference_path}, cand={pair.candidate_path}")
                skipped += 1
                continue

            reid_score, face_score, combined_score = self._compute_scores(ref_img, cand_img)
            predicted_match = combined_score >= threshold

            pair_results.append(PairResult(
                pair=pair,
                reid_score=reid_score,
                face_score=face_score,
                combined_score=combined_score,
                predicted_match=predicted_match,
            ))

        if skipped:
            print(f"[eval] Skipped {skipped}/{len(dataset)} pairs (missing images)")

        # Compute metrics at the given threshold
        result = self._compute_metrics(pair_results, threshold)

        # Compute threshold curve
        result.threshold_curve = self._compute_threshold_curve(pair_results)

        return result

    def _compute_metrics(self, pair_results: list[PairResult], threshold: float) -> EvalResult:
        """Compute classification metrics from pair results."""
        tp = fp = tn = fn = 0

        for pr in pair_results:
            predicted = pr.combined_score >= threshold
            actual = pr.pair.is_match

            if predicted and actual:
                tp += 1
            elif predicted and not actual:
                fp += 1
            elif not predicted and not actual:
                tn += 1
            else:
                fn += 1

        total = tp + fp + tn + fn
        accuracy = (tp + tn) / total if total > 0 else 0.0
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0

        return EvalResult(
            accuracy=accuracy,
            precision=precision,
            recall=recall,
            f1_score=f1,
            false_positive_rate=fpr,
            confusion_matrix={"tp": tp, "fp": fp, "tn": tn, "fn": fn},
            per_pair_results=pair_results,
        )

    def _compute_threshold_curve(
        self, pair_results: list[PairResult]
    ) -> list[tuple[float, float, float]]:
        """Compute precision/recall at thresholds from 0.10 to 0.95 in 0.05 steps."""
        curve = []
        threshold = 0.10
        while threshold <= 0.951:
            tp = fp = fn = 0
            for pr in pair_results:
                predicted = pr.combined_score >= threshold
                actual = pr.pair.is_match
                if predicted and actual:
                    tp += 1
                elif predicted and not actual:
                    fp += 1
                elif not predicted and actual:
                    fn += 1

            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            curve.append((round(threshold, 2), round(precision, 4), round(recall, 4)))
            threshold += 0.05

        return curve
