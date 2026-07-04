import ast
import json
from pathlib import Path

BUNDLE = Path("examples/model-serving/Llama-3.1-8B-Instruct-MLX-8bit")


def test_llama_mlx_8bit_bundle_layout():
    assert (BUNDLE / "README.md").is_file()
    assert (BUNDLE / "config.json").is_file()
    assert (BUNDLE / "requirements.txt").is_file()
    assert (BUNDLE / "reference" / "reference.py").is_file()
    assert (BUNDLE / "reference" / "meta.json").is_file()
    assert (BUNDLE / "reference" / "config.json").is_file()
    assert (BUNDLE / "accuracy_checker" / "checker.py").is_file()
    assert (BUNDLE / "benchmark" / "benchmark.py").is_file()


def test_llama_mlx_8bit_metadata_points_to_target_model():
    meta = json.loads((BUNDLE / "reference" / "meta.json").read_text())
    assert meta["model_id"] == "mlx-community/Meta-Llama-3.1-8B-Instruct-8bit"
    assert meta["revision"] == "142d428004044c37c441272c91316251d9aecc58"

    config = json.loads((BUNDLE / "reference" / "config.json").read_text())
    assert config["model_type"] == "llama"
    assert config["quantization"] == {"group_size": 64, "bits": 8}


def test_llama_mlx_8bit_reference_defaults_to_local_model_dir():
    tree = ast.parse((BUNDLE / "reference" / "reference.py").read_text())
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "DEFAULT_MODEL_DIR"
    ]
    assert assignments, "reference.py should define DEFAULT_MODEL_DIR"
    assert "model" in ast.unparse(assignments[0])


def test_llama_mlx_8bit_checker_and_benchmark_support_url_flags():
    checker_source = (BUNDLE / "accuracy_checker" / "checker.py").read_text()
    benchmark_source = (BUNDLE / "benchmark" / "benchmark.py").read_text()

    assert "--url" in checker_source
    assert "/v1/completions" in checker_source
    assert "response_format" in checker_source
    assert "--dataset-subset" in checker_source
    assert "--dataset-revision" in checker_source
    assert "jsonschema" in checker_source
    assert "load_dataset" in checker_source
    assert "data[0].b64_json" not in checker_source

    assert "--url" in benchmark_source
    assert "--output-json" in benchmark_source
    assert "/v1/completions" in benchmark_source
    assert "response_format" in benchmark_source
    assert "--dataset-subset" in benchmark_source
    assert "--dataset-revision" in benchmark_source


def test_llama_mlx_8bit_pins_jsonschemabench_dataset_revision():
    checker_source = (BUNDLE / "accuracy_checker" / "checker.py").read_text()
    benchmark_source = (BUNDLE / "benchmark" / "benchmark.py").read_text()
    assert "epfl-dlab/JSONSchemaBench" in checker_source
    assert "5bd0f4640badc6f3f02df796421d21cb0ca0b141" in checker_source
    assert "epfl-dlab/JSONSchemaBench" in benchmark_source
