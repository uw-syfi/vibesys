package reading

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"unicode"
)

// Protocol2 reads the record stream a benchmark wrote under evaluator protocol 2 and returns the
// value of the metric the task scores and the other metrics beside it.
//
// The scored metric must be declared by the stream's hello record and declared required, since a
// task cannot rank on a metric a successful run may leave out. A stream that carries an error
// record instead of a result is valid and is returned as a *Reported; every other stream the
// protocol forbids is a *Rejection with the protocol's reason code.
//
// Records are applied in order, and an error record ends the stream: what follows it is not
// read, as PROTOCOL.md says.
func Protocol2(stream []byte, scored string) (Reading, error) {
	var (
		declared map[string]spec
		values   map[string]float64
		position int
		lineNo   int
	)
	for rest := stream; len(rest) > 0; {
		var line []byte
		line, rest, _ = bytes.Cut(rest, []byte{'\n'})
		lineNo++
		if len(bytes.TrimSpace(line)) == 0 {
			continue
		}
		position++
		rec, err := parseLine(line, lineNo)
		if err != nil {
			return Reading{}, err
		}
		switch r := rec.(type) {
		case helloRecord:
			if declared != nil {
				return Reading{}, reject(DuplicateHello, "record %d is a second hello record", position)
			}
			if position != 1 {
				return Reading{}, reject(HelloNotFirst, "record %d is a hello but is not first", position)
			}
			if err := checkHello(r); err != nil {
				return Reading{}, err
			}
			declared = r.metrics
		case errorRecord:
			// An error is the one record that may stand where the hello should be. Anywhere else
			// before the hello it is skipped, since the stream is rejected at its end if no hello
			// arrives, and rejected at the hello if one arrives late.
			if declared == nil && position != 1 {
				continue
			}
			return Reading{}, &Reported{Message: r.message, Requirement: r.requirement}
		case resultRecord:
			if declared == nil {
				continue
			}
			if values != nil {
				return Reading{}, reject(InvalidRecord, "record %d is a second result record, but protocol %d carries one operating point", position, ProtocolVersion)
			}
			if values, err = checkResult(declared, r, position); err != nil {
				return Reading{}, err
			}
		}
	}
	if declared == nil {
		return Reading{}, reject(MissingHello, "the stream has no hello record")
	}
	if values == nil {
		return Reading{}, reject(NoOutcome, "the stream has neither a result nor an error record")
	}
	if s, ok := declared[scored]; !ok || !s.required {
		why := "is not declared by the benchmark"
		if ok {
			why = "is declared optional, so a successful run may omit it and the task cannot rank on it"
		}
		return Reading{}, reject(MissingMetric, "the scored metric %q %s; the hello record declares %s", scored, why, quoted(keys(declared)))
	}
	out := Reading{Value: values[scored]}
	for _, name := range keys(values) {
		if name != scored {
			out.Others = append(out.Others, Named{Name: name, Value: values[name]})
		}
	}
	return out, nil
}

type spec struct{ required bool }

type helloRecord struct {
	protocol int64
	metrics  map[string]spec
}

type resultRecord struct {
	label  string
	values map[string]json.RawMessage
}

type errorRecord struct {
	message     string
	requirement string
}

func checkHello(h helloRecord) error {
	if h.protocol != ProtocolVersion {
		return reject(UnsupportedProtocol, "the hello record has key 'protocol' = %d, but this reader implements version %d", h.protocol, ProtocolVersion)
	}
	if len(h.metrics) == 0 {
		return reject(EmptyMetrics, "the hello record has an empty key 'metrics'")
	}
	for _, name := range keys(h.metrics) {
		if name == "" || strings.IndexFunc(name, isSpace) >= 0 {
			return reject(InvalidMetricName, "the hello record declares metric %q: metric names must be non-empty and contain no whitespace", name)
		}
	}
	return nil
}

func checkResult(declared map[string]spec, r resultRecord, position int) (map[string]float64, error) {
	if r.label != "" {
		return nil, reject(UnsupportedLabel, "record %d has key 'label' = %q, but protocol %d accepts only the default label", position, r.label, ProtocolVersion)
	}
	names := keys(r.values)
	var unknown, missing []string
	for _, name := range names {
		if _, ok := declared[name]; !ok {
			unknown = append(unknown, name)
		}
	}
	if len(unknown) > 0 {
		return nil, reject(UnknownMetric, "record %d has key 'values' with undeclared metrics %s; the hello record declares %s", position, quoted(unknown), quoted(keys(declared)))
	}
	for _, name := range keys(declared) {
		if _, ok := r.values[name]; !ok && declared[name].required {
			missing = append(missing, name)
		}
	}
	if len(missing) > 0 {
		return nil, reject(MissingMetric, "record %d has key 'values' missing required metrics %s", position, quoted(missing))
	}
	row := make(map[string]float64, len(names))
	for _, name := range names {
		raw := r.values[name]
		if !isNumberToken(raw) {
			return nil, reject(NonNumericValue, "record %d has key 'values.%s' = %s, which is not a number", position, name, shorten(raw))
		}
		f, ok := number(string(raw))
		if !ok {
			return nil, reject(NonFiniteValue, "record %d has key 'values.%s' = %s, which is not finite", position, name, shorten(raw))
		}
		row[name] = f
	}
	return row, nil
}

// parseLine reads one line as the record its kind names, rejecting a line that is not a record of
// a known kind, has a key the record does not define, or has a field of the wrong type.
func parseLine(line []byte, lineNo int) (any, error) {
	if len(line) > MaxLineBytes {
		return nil, reject(MalformedLine, "line %d is larger than %d bytes", lineNo, MaxLineBytes)
	}
	var top map[string]json.RawMessage
	if err := json.Unmarshal(line, &top); err != nil {
		var wrongType *json.UnmarshalTypeError
		if errors.As(err, &wrongType) {
			return nil, reject(MalformedLine, "line %d is not a JSON object", lineNo)
		}
		return nil, reject(MalformedLine, "line %d is not valid JSON", lineNo)
	}
	if top == nil {
		return nil, reject(MalformedLine, "line %d is not a JSON object", lineNo)
	}
	var kind string
	if raw, ok := top["kind"]; !ok || json.Unmarshal(raw, &kind) != nil {
		return nil, reject(UnknownKind, "line %d has no record kind", lineNo)
	}
	w := where{line: lineNo, kind: kind}
	switch kind {
	case "hello":
		return parseHello(top, w)
	case "result":
		return parseResult(top, w)
	case "error":
		return parseError(top, w)
	}
	return nil, reject(UnknownKind, "line %d has unknown record kind %q", lineNo, kind)
}

// where names the record a problem is in.
type where struct {
	line int
	kind string
	path string
}

func (w where) in(key string) where {
	if key == "" {
		return w
	}
	if w.path != "" {
		key = w.path + "." + key
	}
	w.path = key
	return w
}

func (w where) unknown(key string) error {
	return reject(UnknownKey, "line %d: %s record has unknown key %q", w.line, w.kind, w.in(key).path)
}

func (w where) invalid(key, why string) error {
	return reject(InvalidRecord, "line %d: %s record has invalid key %q: %s", w.line, w.kind, w.in(key).path, why)
}

// onlyKeys rejects the first key, in order, that the object does not define.
func onlyKeys(m map[string]json.RawMessage, w where, allowed ...string) error {
	for _, k := range keys(m) {
		if !slices.Contains(allowed, k) {
			return w.unknown(k)
		}
	}
	return nil
}

func parseHello(top map[string]json.RawMessage, w where) (any, error) {
	if err := onlyKeys(top, w, "kind", "protocol", "metrics"); err != nil {
		return nil, err
	}
	var h helloRecord
	raw, ok := top["protocol"]
	if !ok {
		return nil, w.invalid("protocol", "required")
	}
	if h.protocol, ok = asInt(raw); !ok {
		return nil, w.invalid("protocol", "not an integer")
	}
	raw, ok = top["metrics"]
	if !ok {
		return nil, w.invalid("metrics", "required")
	}
	metrics, ok := asObject(raw)
	if !ok {
		return nil, w.invalid("metrics", "not an object")
	}
	h.metrics = make(map[string]spec, len(metrics))
	for _, name := range keys(metrics) {
		mw := w.in("metrics").in(name)
		so, ok := asObject(metrics[name])
		if !ok {
			return nil, w.in("metrics").invalid(name, "not an object")
		}
		if err := onlyKeys(so, mw, "unit", "direction", "required"); err != nil {
			return nil, err
		}
		s := spec{required: true}
		if raw, ok := so["unit"]; ok && !isNull(raw) {
			if _, ok := asString(raw); !ok {
				return nil, mw.invalid("unit", "not a string")
			}
		}
		if raw, ok := so["direction"]; ok && !isNull(raw) {
			if d, ok := asString(raw); !ok || (d != "max" && d != "min") {
				return nil, mw.invalid("direction", `not "max" or "min"`)
			}
		}
		if raw, ok := so["required"]; ok {
			if s.required, ok = asBool(raw); !ok {
				return nil, mw.invalid("required", "not a boolean")
			}
		}
		h.metrics[name] = s
	}
	return h, nil
}

func parseResult(top map[string]json.RawMessage, w where) (any, error) {
	if err := onlyKeys(top, w, "kind", "label", "values"); err != nil {
		return nil, err
	}
	var r resultRecord
	if raw, ok := top["label"]; ok {
		if r.label, ok = asString(raw); !ok {
			return nil, w.invalid("label", "not a string")
		}
	}
	raw, ok := top["values"]
	if !ok {
		return nil, w.invalid("values", "required")
	}
	if r.values, ok = asObject(raw); !ok {
		return nil, w.invalid("values", "not an object")
	}
	return r, nil
}

func parseError(top map[string]json.RawMessage, w where) (any, error) {
	if err := onlyKeys(top, w, "kind", "message", "partial", "error_kind", "requirement"); err != nil {
		return nil, err
	}
	var e errorRecord
	raw, ok := top["message"]
	if !ok {
		return nil, w.invalid("message", "required")
	}
	if e.message, ok = asString(raw); !ok || e.message == "" {
		return nil, w.invalid("message", "not a non-empty string")
	}
	if raw, ok := top["partial"]; ok && !isNull(raw) {
		if err := checkPartial(raw, w.in("partial")); err != nil {
			return nil, err
		}
	}
	notMet := false
	if raw, ok := top["error_kind"]; ok {
		k, ok := asString(raw)
		if !ok || (k != "transient" && k != "requirement_not_met") {
			return nil, w.invalid("error_kind", `not "transient" or "requirement_not_met"`)
		}
		notMet = k == "requirement_not_met"
	}
	if raw, ok := top["requirement"]; ok && !isNull(raw) {
		if e.requirement, ok = asName(raw); !ok {
			return nil, w.invalid("requirement", "not a non-empty string without whitespace")
		}
	}
	if notMet != (e.requirement != "") {
		return nil, w.invalid("requirement", `required by, and only allowed with, key 'error_kind' = "requirement_not_met"`)
	}
	return e, nil
}

// checkPartial validates what a failed run measured before it stopped. The reader keeps none of
// it: it never counts as a result.
func checkPartial(raw json.RawMessage, w where) error {
	m, ok := asObject(raw)
	if !ok {
		return w.invalid("", "not an object")
	}
	if err := onlyKeys(m, w, "name", "value", "direction", "unit", "target", "progress"); err != nil {
		return err
	}
	if r, ok := m["name"]; !ok {
		return w.invalid("name", "required")
	} else if _, ok := asName(r); !ok {
		return w.invalid("name", "not a non-empty string without whitespace")
	}
	if r, ok := m["value"]; !ok {
		return w.invalid("value", "required")
	} else if _, ok := asFinite(r); !ok {
		return w.invalid("value", "not a finite number")
	}
	if r, ok := m["direction"]; !ok {
		return w.invalid("direction", "required")
	} else if d, ok := asString(r); !ok || (d != "max" && d != "min") {
		return w.invalid("direction", `not "max" or "min"`)
	}
	if r, ok := m["unit"]; ok && !isNull(r) {
		if u, ok := asString(r); !ok || u == "" {
			return w.invalid("unit", "not a non-empty string")
		}
	}
	if r, ok := m["target"]; ok && !isNull(r) {
		if _, ok := asFinite(r); !ok {
			return w.invalid("target", "not a finite number")
		}
	}
	if r, ok := m["progress"]; ok && !isNull(r) {
		return checkProgress(r, w.in("progress"))
	}
	return nil
}

func checkProgress(raw json.RawMessage, w where) error {
	m, ok := asObject(raw)
	if !ok {
		return w.invalid("", "not an object")
	}
	if err := onlyKeys(m, w, "completed", "required", "unit"); err != nil {
		return err
	}
	if r, ok := m["completed"]; !ok {
		return w.invalid("completed", "required")
	} else if n, ok := asInt(r); !ok || n < 0 {
		return w.invalid("completed", "not an integer of at least 0")
	}
	if r, ok := m["required"]; !ok {
		return w.invalid("required", "required")
	} else if n, ok := asInt(r); !ok || n < 1 {
		return w.invalid("required", "not an integer of at least 1")
	}
	if r, ok := m["unit"]; !ok {
		return w.invalid("unit", "required")
	} else if _, ok := asName(r); !ok {
		return w.invalid("unit", "not a non-empty string without whitespace")
	}
	return nil
}

func asObject(raw json.RawMessage) (map[string]json.RawMessage, bool) {
	if len(raw) == 0 || raw[0] != '{' {
		return nil, false
	}
	var m map[string]json.RawMessage
	if json.Unmarshal(raw, &m) != nil || m == nil {
		return nil, false
	}
	return m, true
}

func asString(raw json.RawMessage) (string, bool) {
	var s string
	if len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &s) != nil {
		return "", false
	}
	return s, true
}

// asName reads a non-empty string without whitespace.
func asName(raw json.RawMessage) (string, bool) {
	s, ok := asString(raw)
	if !ok || s == "" || strings.IndexFunc(s, isSpace) >= 0 {
		return "", false
	}
	return s, true
}

func asBool(raw json.RawMessage) (bool, bool) {
	switch string(raw) {
	case "true":
		return true, true
	case "false":
		return false, true
	}
	return false, false
}

// asInt reads an integer token. A number written with a fraction or an exponent is not one, as in
// the protocol's strict records.
func asInt(raw json.RawMessage) (int64, bool) {
	var n int64
	if !isNumberToken(raw) || json.Unmarshal(raw, &n) != nil {
		return 0, false
	}
	return n, true
}

func asFinite(raw json.RawMessage) (float64, bool) {
	if !isNumberToken(raw) {
		return 0, false
	}
	return number(string(raw))
}

func isNull(raw json.RawMessage) bool { return string(raw) == "null" }

// isSpace is Unicode white space, with the C0 separators the protocol's other readers count.
func isSpace(r rune) bool { return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f) }

func keys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	slices.Sort(out)
	return out
}

func quoted(names []string) string {
	q := make([]string, len(names))
	for i, n := range names {
		q[i] = fmt.Sprintf("%q", n)
	}
	return strings.Join(q, ", ")
}

// shorten bounds a value quoted in an error, since the value is the benchmark's own.
func shorten(raw []byte) string {
	const limit = 40
	if len(raw) > limit {
		return string(raw[:limit]) + "..."
	}
	return string(raw)
}
