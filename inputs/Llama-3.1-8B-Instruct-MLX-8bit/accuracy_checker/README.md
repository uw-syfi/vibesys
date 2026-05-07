Accuracy checker inputs for the MLX 8-bit Llama JSON-schema target.

Run against a local server:

```bash
python checker.py --url http://localhost:8000 --max-tokens 256
```

The checker sends deterministic `/v1/completions` requests with schemas from
`epfl-dlab/JSONSchemaBench`, pinned to revision
`5bd0f4640badc6f3f02df796421d21cb0ca0b141`, and
`response_format: {"type": "json_schema", ...}`. It verifies that each response
parses as JSON and validates against the schema. For schemas that can contain
strings, it also injects a random sentinel token into the prompt and requires
the output to include it, which prevents fixed-template or schema-only shortcuts
from passing.

The default subset is `full` and default split is `val`. Use
`--dataset-subset`, `--split`, `--limit`, and `--dataset-cache-dir` to control
the dataset slice and cache location.
