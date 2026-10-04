"""Guard the TypeScript quality rules enabled in the clients Biome configs.

Biome enforces these rules in CI, so this test does not re-check TypeScript
sources. It pins the rules and thresholds themselves: dropping one, or
loosening a threshold, is a deliberate decision that should show up as a
failing test rather than as silently vanished coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_RULES = {
    ("complexity", "noExcessiveCognitiveComplexity"): {"maxAllowedComplexity": 15},
    ("complexity", "noExcessiveLinesPerFunction"): {"maxLines": 80, "skipBlankLines": True},
    ("complexity", "useMaxParams"): {"max": 6},
    ("style", "noExcessiveLinesPerFile"): {"maxLines": 2000},
}

# Kept in step with the Python side: `[tool.vibesys.file_length]` in
# pyproject.toml uses the same ceiling and the same test-file exemption.
TEST_FILE_MAX_LINES = 10_000
PRODUCTION_FILE_WARNING_LINES = 1_500


def load_biome_config() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "clients" / "biome.json").read_text(encoding="utf-8"))


def load_biome_warning_config() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "clients" / "biome.warnings.json").read_text(encoding="utf-8"))


def test_quality_rules_are_errors_with_the_documented_thresholds() -> None:
    rules = load_biome_config()["linter"]["rules"]

    for (group, name), options in EXPECTED_RULES.items():
        entry = rules[group][name]
        assert entry["level"] == "error", f"{group}/{name} must fail CI, not warn"
        assert entry["options"] == options


def test_test_files_have_a_finite_file_cap_and_only_exempt_function_length() -> None:
    overrides = load_biome_config()["overrides"]
    matching = [entry for entry in overrides if "**/*.test.ts" in entry["includes"]]
    assert len(matching) == 1

    exempt = matching[0]["linter"]["rules"]
    assert exempt["complexity"]["noExcessiveLinesPerFunction"] == "off"
    assert exempt["style"]["noExcessiveLinesPerFile"] == {
        "level": "error",
        "options": {"maxLines": TEST_FILE_MAX_LINES},
    }

    # Cognitive complexity and parameter counts still apply to test files.
    assert "noExcessiveCognitiveComplexity" not in exempt.get("complexity", {})
    assert "useMaxParams" not in exempt.get("complexity", {})


def test_production_files_warn_before_the_hard_cap() -> None:
    config = load_biome_warning_config()
    assert config["extends"] == ["./biome.json"]
    assert "!**/*.test.ts" in config["files"]["includes"]
    assert "!**/*.test.tsx" in config["files"]["includes"]
    assert config["linter"]["rules"]["style"]["noExcessiveLinesPerFile"] == {
        "level": "warn",
        "options": {"maxLines": PRODUCTION_FILE_WARNING_LINES},
    }
    assert (
        EXPECTED_RULES[("style", "noExcessiveLinesPerFile")]["maxLines"]
        > PRODUCTION_FILE_WARNING_LINES
    )
