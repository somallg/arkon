"""
Vision model catalog — code-level whitelist of supported vision/image models.

Used by the image-captioning task during document ingestion. Captures cost
metadata so the cost dashboard can attribute spend per source.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class VisionModelSpec:
    id: str                          # canonical "<provider>/<model_id>"
    provider: str                    # "openai" | "google"
    model_id: str                    # ID sent to the provider API
    max_image_size_mb: int           # provider's per-image size cap
    label: str
    cost_per_1m_input_tokens: Optional[float]
    cost_per_image: Optional[float]  # USD per image when provider charges flat
    notes: Optional[str] = None


VISION_CATALOG: dict[str, VisionModelSpec] = {
    # --- Google Gemini ---
    "google/gemini-3.5-flash": VisionModelSpec(
        id="google/gemini-3.5-flash",
        provider="google",
        model_id="gemini-3.5-flash",
        max_image_size_mb=20,
        label="Gemini 3.5 Flash",
        cost_per_1m_input_tokens=None,
        cost_per_image=None,
        notes=(
            "Frontier-class performance Gemini Flash for multimodal understanding "
            "(richer visual reasoning)."
        ),
    ),
    "google/gemini-3.1-flash-lite": VisionModelSpec(
        id="google/gemini-3.1-flash-lite",
        provider="google",
        model_id="gemini-3.1-flash-lite",
        max_image_size_mb=20,
        label="Gemini 3.1 Flash-Lite",
        cost_per_1m_input_tokens=0.25,
        cost_per_image=None,
        notes="Most cost-efficient Gemini for image captioning. Recommended default for high-volume ingestion.",
    ),
    "google/gemini-2.5-flash": VisionModelSpec(
        id="google/gemini-2.5-flash",
        provider="google",
        model_id="gemini-2.5-flash",
        max_image_size_mb=20,
        label="Gemini 2.5 Flash",
        cost_per_1m_input_tokens=0.075,
        cost_per_image=None,
    ),
    # --- OpenAI ---
    "openai/gpt-4o": VisionModelSpec(
        id="openai/gpt-4o",
        provider="openai",
        model_id="gpt-4o",
        max_image_size_mb=20,
        label="GPT-4o",
        cost_per_1m_input_tokens=2.50,
        cost_per_image=None,
        notes="Highest-quality vision. Use for tricky diagrams.",
    ),
    "openai/gpt-4o-mini": VisionModelSpec(
        id="openai/gpt-4o-mini",
        provider="openai",
        model_id="gpt-4o-mini",
        max_image_size_mb=20,
        label="GPT-4o Mini",
        cost_per_1m_input_tokens=0.15,
        cost_per_image=None,
    ),
}


class UnknownVisionModel(KeyError):
    """Raised when a spec_id is not in the vision catalog."""


def get_spec(spec_id: str) -> VisionModelSpec:
    try:
        return VISION_CATALOG[spec_id]
    except KeyError as e:
        raise UnknownVisionModel(
            f"Unknown vision model spec_id={spec_id!r}. "
            f"Valid IDs: {sorted(VISION_CATALOG.keys())}"
        ) from e


def list_specs() -> list[VisionModelSpec]:
    return list(VISION_CATALOG.values())


def list_specs_by_provider(provider: str) -> list[VisionModelSpec]:
    return [s for s in VISION_CATALOG.values() if s.provider == provider]


def derive_spec_id(provider: str, model_id: str) -> Optional[str]:
    """Look up a spec_id from legacy (provider, model_id) config."""
    for spec in VISION_CATALOG.values():
        if spec.provider == provider and spec.model_id == model_id:
            return spec.id
    return None
