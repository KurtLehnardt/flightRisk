"""Report generation for evaluation results.

Produces human-readable text summaries and machine-readable JSON output.
"""

import json
from pathlib import Path

from .runner import EvalResult


def generate_text_report(result: EvalResult) -> str:
    """Generate a formatted text summary of evaluation results.

    Args:
        result: EvalResult from an evaluation run.

    Returns:
        Multi-line string with metrics summary.
    """
    cm = result.confusion_matrix
    total = cm["tp"] + cm["fp"] + cm["tn"] + cm["fn"]

    lines = [
        "=" * 60,
        "  Amber Drone — Match Evaluation Report",
        "=" * 60,
        "",
        f"  Total pairs evaluated:  {total}",
        f"  True positives:         {cm['tp']}",
        f"  False positives:        {cm['fp']}",
        f"  True negatives:         {cm['tn']}",
        f"  False negatives:        {cm['fn']}",
        "",
        "  Metrics:",
        f"    Accuracy:             {result.accuracy:.4f}",
        f"    Precision:            {result.precision:.4f}",
        f"    Recall:               {result.recall:.4f}",
        f"    F1 Score:             {result.f1_score:.4f}",
        f"    False Positive Rate:  {result.false_positive_rate:.4f}",
        "",
    ]

    if result.threshold_curve:
        lines.append("  Threshold Curve:")
        lines.append(f"    {'Threshold':>10}  {'Precision':>10}  {'Recall':>10}")
        lines.append(f"    {'-' * 10}  {'-' * 10}  {'-' * 10}")
        for threshold, precision, recall in result.threshold_curve:
            lines.append(f"    {threshold:>10.2f}  {precision:>10.4f}  {recall:>10.4f}")
        lines.append("")

    if result.per_pair_results:
        lines.append("  Per-Pair Results:")
        lines.append(f"    {'#':>3}  {'ReID':>6}  {'Face':>6}  {'Combined':>8}  {'Pred':>5}  {'Actual':>6}  {'Match Type'}")
        lines.append(f"    {'---':>3}  {'------':>6}  {'------':>6}  {'--------':>8}  {'-----':>5}  {'------':>6}  {'----------'}")
        for i, pr in enumerate(result.per_pair_results, 1):
            pred = "YES" if pr.predicted_match else "no"
            actual = "YES" if pr.pair.is_match else "no"
            correct = "  " if pr.predicted_match == pr.pair.is_match else "!!"
            lines.append(
                f"    {i:>3}  {pr.reid_score:>6.3f}  {pr.face_score:>6.3f}  "
                f"{pr.combined_score:>8.3f}  {pred:>5}  {actual:>6}  {pr.pair.match_type} {correct}"
            )
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def generate_json_report(result: EvalResult, path: str) -> None:
    """Save evaluation results as JSON.

    Args:
        result: EvalResult from an evaluation run.
        path: Output file path.
    """
    data = {
        "metrics": {
            "accuracy": result.accuracy,
            "precision": result.precision,
            "recall": result.recall,
            "f1_score": result.f1_score,
            "false_positive_rate": result.false_positive_rate,
        },
        "confusion_matrix": result.confusion_matrix,
        "threshold_curve": [
            {"threshold": t, "precision": p, "recall": r}
            for t, p, r in result.threshold_curve
        ],
        "per_pair_results": [
            {
                "reference": pr.pair.reference_path,
                "candidate": pr.pair.candidate_path,
                "is_match": pr.pair.is_match,
                "match_type": pr.pair.match_type,
                "reid_score": pr.reid_score,
                "face_score": pr.face_score,
                "combined_score": pr.combined_score,
                "predicted_match": pr.predicted_match,
            }
            for pr in result.per_pair_results
        ],
    }

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[eval] JSON report saved to {path}")
