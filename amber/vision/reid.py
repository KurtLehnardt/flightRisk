"""Person Re-Identification using feature embeddings.

Compares detected persons against a reference photo of the target child.
Uses a lightweight CNN (ResNet18 or MobileNetV2) to extract appearance
embeddings, then cosine similarity for matching.
"""

import numpy as np
import cv2

try:
    import torch
    import torchvision.transforms as T
    from torchvision import models
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False


class PersonReID:
    """Extracts appearance embeddings and matches against a target person."""

    def __init__(self, match_threshold: float = 0.55):
        """Initialize ReID with a lightweight feature extractor.

        Args:
            match_threshold: Cosine similarity threshold for a match (0-1).
                             Lower = more permissive, higher = stricter.
        """
        if not HAS_TORCH:
            raise RuntimeError("PyTorch required. pip install torch torchvision")

        self.match_threshold = match_threshold
        self.device = self._select_device()

        # Use MobileNetV2 — lightweight, fast, good enough for ReID
        self.model = models.mobilenet_v2(weights=models.MobileNet_V2_Weights.DEFAULT)
        # Remove the classifier, keep feature extractor (1280-d output)
        self.model.classifier = torch.nn.Identity()
        self.model = self.model.to(self.device)
        self.model.eval()

        self.transform = T.Compose([
            T.ToPILImage(),
            T.Resize((224, 224)),
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])

        self._target_embedding: np.ndarray | None = None
        print(f"[reid] MobileNetV2 loaded on {self.device}")

    def _select_device(self) -> str:
        if torch.backends.mps.is_available():
            return "mps"
        elif torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def _extract_embedding(self, image: np.ndarray) -> np.ndarray:
        """Extract a feature embedding from a person crop.

        Args:
            image: BGR numpy array of a cropped person.

        Returns:
            Normalized 1280-d feature vector.
        """
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        tensor = self.transform(rgb).unsqueeze(0).to(self.device)

        with torch.no_grad():
            embedding = self.model(tensor).cpu().numpy().flatten()

        # L2 normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm
        return embedding

    def set_target(self, image: np.ndarray):
        """Set the reference image of the person to find.

        Args:
            image: BGR numpy array — a photo of the target child.
        """
        self._target_embedding = self._extract_embedding(image)
        print(f"[reid] Target embedding set ({self._target_embedding.shape[0]}-d)")

    def set_target_from_file(self, path: str):
        """Load a reference photo from disk."""
        image = cv2.imread(path)
        if image is None:
            raise FileNotFoundError(f"Cannot read image: {path}")
        self.set_target(image)

    def compare(self, crop: np.ndarray) -> float:
        """Compare a detected person crop against the target.

        Args:
            crop: BGR numpy array of a detected person.

        Returns:
            Cosine similarity score (0-1). Higher = more similar.
        """
        if self._target_embedding is None:
            return 0.0

        embedding = self._extract_embedding(crop)
        similarity = float(np.dot(self._target_embedding, embedding))
        return max(0.0, similarity)  # clamp to 0-1

    def find_match(self, detections: list[dict]) -> tuple[int | None, float]:
        """Find the best match among detected persons.

        Args:
            detections: List of dicts with 'crop' key from PersonDetector.

        Returns:
            (index, score) of best match, or (None, 0.0) if no match.
        """
        if self._target_embedding is None or not detections:
            return None, 0.0

        best_idx = None
        best_score = 0.0

        for i, det in enumerate(detections):
            crop = det.get("crop")
            if crop is None or crop.size == 0:
                continue
            score = self.compare(crop)
            if score > best_score:
                best_score = score
                best_idx = i

        if best_score >= self.match_threshold:
            return best_idx, best_score

        return None, best_score
