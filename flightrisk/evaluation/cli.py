"""CLI for running match accuracy evaluations.

Usage:
    python -m flightrisk.evaluation --dataset path/to/labels.json --threshold 0.5
    python -m flightrisk.evaluation --dataset path/to/labels.json --output results.json
    python -m flightrisk.evaluation --create-sample --output eval_data/sample/
    python -m flightrisk.evaluation detect --dataset datasets/heridal --tiled 1 --output results.json
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
        from flightrisk.vision.reid import PersonReID
        reid = PersonReID()
    except Exception as e:
        print(f"[eval] ReID unavailable: {e}")

    try:
        from flightrisk.vision.face import FaceRecognizer
        face = FaceRecognizer()
    except Exception as e:
        print(f"[eval] Face recognition unavailable: {e}")

    try:
        from flightrisk.vision.scorer import MatchScorer
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


def run_detect(argv: list[str]) -> None:
    """Run the altitude benchmark: recall and precision by person pixel height."""
    from .detection import evaluate, load_voc_dir, min_person_px

    parser = argparse.ArgumentParser(
        prog="flightrisk.evaluation detect",
        description="Measure person detection recall by pixel height on a VOC dataset",
    )
    parser.add_argument("--dataset", required=True, help="Directory of VOC XML and images")
    parser.add_argument("--tiled", type=int, choices=(0, 1), default=0,
                        help="1 = tiled inference, 0 = single full-frame pass")
    parser.add_argument("--output", default=None, help="Path to save JSON results")
    args = parser.parse_args(argv)

    from flightrisk.vision.detector import PersonDetector

    samples = load_voc_dir(args.dataset)
    if not samples:
        print(f"[eval] ERROR: no VOC annotations with images found in {args.dataset}")
        sys.exit(1)
    print(f"[eval] Loaded {len(samples)} images from {args.dataset}")

    detector = PersonDetector(tiled=bool(args.tiled))
    result = evaluate(detector, samples)
    threshold = min_person_px(result)

    print("bin_px        n_gt   recall  precision")
    for b in result.bins:
        hi = "inf" if b.hi == float("inf") else f"{b.hi:g}"
        print(f"[{b.lo:g},{hi})".ljust(12), f"{b.n_gt:6d}  {b.recall:6.3f}  {b.precision:9.3f}")
    print(f"min_person_px (recall >= 0.5): {threshold}")

    if args.output:
        payload = result.to_dict()
        payload["tiled"] = bool(args.tiled)
        payload["min_person_px"] = threshold
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"[eval] Results saved to {args.output}")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "detect":
        run_detect(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        prog="flightrisk.evaluation",
        description="Evaluate match accuracy for FlightRisk vision pipeline",
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
