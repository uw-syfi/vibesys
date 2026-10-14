import sys
from pathlib import Path

from tests.support import run_test_command

IMAGE = Path("examples/model-serving/images/rocm-mi30x-engine-free")
BASE_TAG = "lmsysorg/sglang:v0.5.18-rocm700-mi30x"
ENGINE_PACKAGES = ("sglang", "sgl-kernel", "sglang-router", "sgl-router", "vllm", "tensorrt_llm")
KEPT_PACKAGES = (
    "torch",
    "triton",
    "aiter",
    "transformers",
    "safetensors",
    "aiohttp",
    "fastapi",
    "uvicorn",
    "numpy",
)
SITE_STRINGS = ("cscs", "beverin", "capstor", "viclsh")


def test_dockerfile_pins_base_and_uninstalls_engines() -> None:
    text = (IMAGE / "Dockerfile").read_text()
    assert f"FROM {BASE_TAG}" in text
    for pkg in ENGINE_PACKAGES:
        assert pkg in text
    assert "pip uninstall -y" in text
    assert "pip-autoremove" not in text
    assert "pip install" not in text
    assert "COPY verify_image.py" in text
    assert "RUN python3 /opt/verify_image.py" in text


def test_image_files_are_site_config_free() -> None:
    for name in ("Dockerfile", "README.md", "verify_image.py"):
        text = (IMAGE / name).read_text().lower()
        for needle in SITE_STRINGS:
            assert needle not in text, f"{needle} in {name}"


def test_readme_documents_placeholders() -> None:
    text = (IMAGE / "README.md").read_text()
    for placeholder in ("<SCRATCH>", "<SQSH_PATH>", "<MODEL_DIR>"):
        assert placeholder in text
    assert "command_prefix" in text


def test_verify_script_fails_closed_outside_the_image() -> None:
    # Isolated and without site-packages, so no kept package can import: the host's own
    # packages (a torch install costs seconds to import) cannot change the outcome.
    proc = run_test_command(  # fixed argv, no untrusted input
        [sys.executable, "-I", "-S", str(IMAGE / "verify_image.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    for package in KEPT_PACKAGES:
        assert f"kept package failed to import: {package}" in proc.stderr
    assert "engine still importable" not in proc.stderr
