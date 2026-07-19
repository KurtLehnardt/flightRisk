"""Evaluation dataset for labeled image pairs.

Manages collections of reference/candidate pairs with ground-truth labels
for measuring match accuracy across ReID, face recognition, and scoring.
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator


@dataclass
class EvalPair:
    """A labeled pair of images for evaluation."""

    reference_path: str
    candidate_path: str
    is_match: bool
    match_type: str  # "same_person", "same_clothing", "different_person", "similar_looking"
    notes: str = ""

    def __post_init__(self):
        valid_types = {"same_person", "same_clothing", "different_person", "similar_looking"}
        if self.match_type not in valid_types:
            raise ValueError(f"match_type must be one of {valid_types}, got '{self.match_type}'")


class EvalDataset:
    """Collection of labeled image pairs for evaluation."""

    def __init__(self, name: str = "", description: str = ""):
        self.name = name
        self.description = description
        self._pairs: list[EvalPair] = []

    @classmethod
    def load_from_json(cls, path: str) -> "EvalDataset":
        """Load dataset from a JSON manifest file.

        The JSON should have:
            name: str
            description: str
            pairs: list of {reference, candidate, is_match, match_type, notes}

        Image paths in the manifest are resolved relative to the manifest file.
        """
        manifest = Path(path)
        with open(manifest) as f:
            data = json.load(f)

        dataset = cls(name=data.get("name", ""), description=data.get("description", ""))
        base_dir = manifest.parent

        for p in data.get("pairs", []):
            pair = EvalPair(
                reference_path=str(base_dir / p["reference"]),
                candidate_path=str(base_dir / p["candidate"]),
                is_match=p["is_match"],
                match_type=p["match_type"],
                notes=p.get("notes", ""),
            )
            dataset.add_pair(pair)

        return dataset

    def save_to_json(self, path: str) -> None:
        """Save dataset to a JSON manifest file."""
        data = {
            "name": self.name,
            "description": self.description,
            "pairs": [
                {
                    "reference": p.reference_path,
                    "candidate": p.candidate_path,
                    "is_match": p.is_match,
                    "match_type": p.match_type,
                    "notes": p.notes,
                }
                for p in self._pairs
            ],
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def add_pair(self, pair: EvalPair) -> None:
        """Add a labeled pair to the dataset."""
        self._pairs.append(pair)

    def __iter__(self) -> Iterator[EvalPair]:
        return iter(self._pairs)

    def __len__(self) -> int:
        return len(self._pairs)
