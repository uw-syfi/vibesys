package reading

import (
	"bytes"
	"errors"
	"fmt"
	"slices"
	"strings"
	"unicode"

	"buf.build/go/protovalidate"

	evaluatorv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/evaluator/v1"
)

// Protocol2 reads the record stream a benchmark wrote under evaluator protocol 2 and returns the
// value of the metric the task scores and the other metrics beside it.
//
// The scored metric must be declared by the stream's hello record and declared required, since a
// task cannot rank on a metric a successful run may leave out. A stream that carries an error
// record instead of a result is valid and is returned as a *Reported; every other stream the
// protocol forbids is a *Rejection with the protocol's reason code.
//
// Each line is a evaluatorv1.ResultRecord, the protocol's one definition. Records are applied in
// order, and an error record ends the stream: what follows it is not read, as PROTOCOL.md says.
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
	protocol uint32
	metrics  map[string]spec
}

type resultRecord struct {
	label  string
	values map[string]float64
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
	return r.values, nil
}

// parseLine reads one line as the record its kind names, rejecting a line that is not a record of
// a known kind, has a key the record does not define, or has a field of the wrong type. The
// definition is evaluatorv1.ResultRecord: decodeObject fits the line to it and its validation
// rules say what each kind may hold, so this function only chooses which failure to report.
func parseLine(line []byte, lineNo int) (any, error) {
	if len(line) > MaxLineBytes {
		return nil, reject(MalformedLine, "line %d is larger than %d bytes", lineNo, MaxLineBytes)
	}
	rec := &evaluatorv1.ResultRecord{}
	problems, err := decodeObject(line, rec.ProtoReflect())
	if err != nil {
		if errors.Is(err, errNotObject) {
			return nil, reject(MalformedLine, "line %d is not a JSON object", lineNo)
		}
		return nil, reject(MalformedLine, "line %d is not valid JSON", lineNo)
	}
	violations, err := violationsOf(rec)
	if err != nil {
		return nil, err
	}
	// A line of no known kind cannot be said to have a wrong key, so that is reported first.
	for _, v := range violations {
		if v.path == "kind" {
			if rec.Kind == nil {
				return nil, reject(UnknownKind, "line %d has no record kind", lineNo)
			}
			return nil, reject(UnknownKind, "line %d has unknown record kind %q", lineNo, rec.GetKind())
		}
	}
	kind := rec.GetKind()
	if err := firstProblem(problems, violations, lineNo, kind); err != nil {
		return nil, err
	}
	switch kind {
	case "hello":
		h := helloRecord{protocol: rec.GetProtocol(), metrics: make(map[string]spec, len(rec.GetMetrics()))}
		for name, s := range rec.GetMetrics() {
			h.metrics[name] = spec{required: s.Required == nil || s.GetRequired()}
		}
		return h, nil
	case "result":
		return resultRecord{label: rec.GetLabel(), values: rec.GetValues()}, nil
	default:
		return errorRecord{message: rec.GetMessage(), requirement: rec.GetRequirement()}, nil
	}
}

// violation is one rule of the definition that the record broke.
type violation struct {
	path string
	rule string
	why  string
}

// violationsOf checks the rules the definition states, ordered so the choice among several is
// the same every time.
func violationsOf(rec *evaluatorv1.ResultRecord) ([]violation, error) {
	err := protovalidate.Validate(rec)
	if err == nil {
		return nil, nil
	}
	var invalid *protovalidate.ValidationError
	if !errors.As(err, &invalid) {
		return nil, fmt.Errorf("checking the result record against its definition: %w", err)
	}
	out := make([]violation, 0, len(invalid.Violations))
	for _, v := range invalid.Violations {
		out = append(out, violation{path: protovalidate.FieldPathString(v.Proto.GetField()), rule: v.Proto.GetRuleId(), why: v.Proto.GetMessage()})
	}
	slices.SortFunc(out, func(a, b violation) int {
		return strings.Compare(a.path+"\x00"+a.rule, b.path+"\x00"+b.rule)
	})
	return out, nil
}

// firstProblem reports the failure to put first: a key the record does not define, then a value
// of the wrong type, then a rule of the definition.
func firstProblem(problems []problem, violations []violation, line int, kind string) error {
	slices.SortStableFunc(problems, func(a, b problem) int { return strings.Compare(a.path, b.path) })
	for _, p := range problems {
		if p.kind == unknownKey {
			return reject(UnknownKey, "line %d: %s record has unknown key %q", line, kind, p.path)
		}
	}
	for _, v := range violations {
		if key, ok := strings.CutPrefix(v.rule, "unknown_key."); ok {
			return reject(UnknownKey, "line %d: %s record has unknown key %q", line, kind, key)
		}
	}
	for _, p := range problems {
		if !strings.HasPrefix(p.path, "values.") {
			return reject(InvalidRecord, "line %d: %s record has invalid key %q: not %s", line, kind, p.path, p.want)
		}
		code := NonNumericValue
		if p.kind == notFinite {
			code = NonFiniteValue
		}
		return reject(code, "line %d: %s record has key %q, which is not %s", line, kind, p.path, p.want)
	}
	for _, v := range violations {
		key := v.path
		if k, ok := strings.CutPrefix(v.rule, "required."); ok {
			key = k
		} else if strings.HasPrefix(v.rule, "requirement.") {
			key = "requirement"
		}
		return reject(InvalidRecord, "line %d: %s record has invalid key %q: %s", line, kind, key, v.why)
	}
	return nil
}

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

// isSpace is Unicode white space, with the C0 separators the protocol's other readers count.
func isSpace(r rune) bool { return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f) }
