# Constrained JSON Accuracy Checker

The checker sends streaming `/v1/completions` requests with `guided_json` and
validates each returned text with `json.loads` and `jsonschema`. Each case
contains a fresh sentinel value that must appear in the schema-valid `sentinel`
field, which prevents hard-coded schema-only outputs.
