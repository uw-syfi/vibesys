package reading

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
)

// JSONField reads the file a benchmark wrote under a task's [benchmark.result] contract: one JSON
// object whose top-level field holds the metric. A list of objects is accepted only when exactly
// one entry carries the field, since the run has one value of the metric and no rule says which of
// several to take. The value must be a finite number: a boolean, a string, null, or a number that
// overflows is a failed run.
func JSONField(data []byte, field string) (Reading, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return Reading{}, reject(MalformedFile, "the result file is empty")
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var top any
	if err := dec.Decode(&top); err != nil {
		return Reading{}, reject(MalformedFile, "the result file is not valid JSON: %v", err)
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return Reading{}, reject(MalformedFile, "the result file holds more than one JSON value")
	}

	var entries []map[string]any
	switch v := top.(type) {
	case map[string]any:
		entries = []map[string]any{v}
	case []any:
		for _, e := range v {
			if o, ok := e.(map[string]any); ok {
				entries = append(entries, o)
			}
		}
	default:
		return Reading{}, reject(MalformedFile, "the result file is neither a JSON object nor a list of objects")
	}

	var carrying []any
	for _, e := range entries {
		if v, ok := e[field]; ok {
			carrying = append(carrying, v)
		}
	}
	switch len(carrying) {
	case 0:
		return Reading{}, reject(MissingMetric, "the result file has no field %q", field)
	case 1:
	default:
		return Reading{}, reject(AmbiguousMetric, "%d entries of the result file carry the field %q; expected exactly one", len(carrying), field)
	}
	n, ok := carrying[0].(json.Number)
	if !ok {
		return Reading{}, reject(NonNumericValue, "the field %q is not a number", field)
	}
	value, ok := number(n.String())
	if !ok {
		return Reading{}, reject(NonFiniteValue, "the field %q = %s is not finite", field, n)
	}
	return Reading{Value: value}, nil
}
