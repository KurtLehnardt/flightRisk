"""CLI for running match accuracy evaluations.

Usage:
    python -m amber.evaluation --dataset path/to/labels.json --threshold 0.5
    python -m amber.evaluation --dataset path/to/labels.json --output results.json
    python -m amber.evaluation --create-sample --output eval_data/sample/
"""

import argparse
import json
import sys
from pathlib import Path


def create_sample_dataset(output_dir: str) -> None:
    """Create a sample dataset directory with a labels.json manifest."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": "Sample Evaluation Dataset",
        "description": "Placeholder — replace with real test images",
        "pairs": [
            {
                "reference": "reference.jpg",
                "candidate": "candidate_match.jpg",
                "is_match": True,
                "match_type": "same_person",
                "notes": "Same person, same clothing",
            },
            {
                "reference": "reference.jpg",
                "candidate": "candidate_nomatch.jpg",
                "is_match": False,
                "match_type": "different_person",
                "notes": "Different person",
            },
        ],
    }

    labels_path = out / "labels.json"
    with open(labels_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Sample dataset created at {labels_path}")
    print(f"Add test images to {out}/ and update labels.json accordingly.")


def run_evaluation(dataset_path: str, output_path: str | None, threshold: float) -> None:
    """Run evaluation pipeline on a labeled dataset."""
    from .dataset import EvalDataset
    from .runner import EvalRunner
    from .report import generate_text_report, generate_json_report

    # Try to import pipeline components (graceful degradation)
    reid = None
    face = None
    scorer = None

    try:
        from amber.vision.reid import PersonReID
        reid = PersonReID()
    except Exception as e:
        print(f"[eval] ReID unavailable: {e}")

    try:
        from amber.vision.face import FaceRecognizer
        face = FaceRecognizer()
    except Exception as e:
        print(f"[eval] Face recognition unavailable: {e}")

    try:
        from amber.vision.scorer import MatchScorer
        scorer = MatchScorer()
    except Exception as e:
        print(f"[eval] Scorer unavailable: {e}")

    if reid is None and face is None:
        print("[eval] ERROR: Neither ReID nor face recognition available.")
        print("       Install dependencies: pip install torch torchvision insightface onnxruntime")
        sys.exit(1)

    dataset = EvalDataset.load_from_json(dataset_path)
    print(f"[eval] Loaded {len(dataset)} pairs from {dataset_path}")

    runner = EvalRunner(reid=reid, face=face, scorer=scorer)
    result = runner.run(dataset, threshold=threshold)

    # Print text report
    report = generate_text_report(result)
    print(report)

    # Save JSON if requested
    if output_path:
        generate_json_report(result, output_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="amber.evaluation",
        description="Evaluate match accuracy for Amber Drone vision pipeline",
    )

    parser.add_argument(
        "--dataset",
        type=str,
        help="Path to dataset labels.json manifest",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to save JSON results (or output directory for --create-sample)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Combined score threshold for match prediction (default: 0.5)",
    )
    parser.add_argument(
        "--create-sample",
        action="store_true",
        help="Create a sample dataset directory structure",
    )

    args = parser.parse_args()

    if args.create_sample:
        output_dir = args.output or "eval_data/sample"
        create_sample_dataset(output_dir)
        return

    if not args.dataset:
        parser.error("--dataset is required (or use --create-sample)")

    run_evaluation(args.dataset, args.output, args.threshold)


if __name__ == "__main__":
    main()
