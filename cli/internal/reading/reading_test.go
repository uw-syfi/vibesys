package reading_test

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/reading"
)

const fixtures = "../../../sdk/vs-evaluator/fixtures"

type expectations struct {
	Invalid map[string]string `json:"invalid"`
	Valid   map[string]struct {
		Outcome string             `json:"outcome"`
		Values  map[string]float64 `json:"values"`
	} `json:"valid"`
}

func corpus(t testing.TB) (expectations, func(kind, name string) []byte) {
	t.Helper()
	text, err := os.ReadFile(filepath.Join(fixtures, "expectations.json"))
	if err != nil {
		t.Fatalf("the protocol's fixture corpus is the specification and must be there: %v", err)
	}
	var e expectations
	if err := json.Unmarshal(text, &e); err != nil {
		t.Fatal(err)
	}
	return e, func(kind, name string) []byte {
		data, err := os.ReadFile(filepath.Join(fixtures, kind, name+".jsonl"))
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
}

func rejection(t testing.TB, err error) *reading.Rejection {
	t.Helper()
	var r *reading.Rejection
	if !errors.As(err, &r) {
		t.Fatalf("error %v is not a *reading.Rejection", err)
	}
	return r
}

func TestEveryInvalidFixtureIsRejectedWithItsCode(t *testing.T) {
	e, read := corpus(t)
	entries, _ := os.ReadDir(filepath.Join(fixtures, "invalid"))
	if len(entries) != len(e.Invalid) {
		t.Fatalf("the corpus has %d invalid streams and %d expectations", len(entries), len(e.Invalid))
	}
	for name, code := range e.Invalid {
		_, err := reading.Protocol2(read("invalid", name), "total_ops_per_sec")
		if err == nil {
			t.Errorf("%s: accepted", name)
			continue
		}
		if r := rejection(t, err); string(r.Code) != code || r.Message == "" {
			t.Errorf("%s: %s %q, want %s", name, r.Code, r.Message, code)
		}
	}
}

// scoredIn names, for each valid fixture whose outcome is a row, the metric a task would score.
var scoredIn = map[string]string{
	"bare-metric-spec":            "primary_value",
	"negative-and-integer-values": "drift_ratio",
	"optional-metric-absent":      "operations_per_second",
	"optional-metric-present":     "operations_per_second",
	"single-metric":               "total_ops_per_sec",
	"two-metrics":                 "aggregate_throughput",
}

func TestEveryValidFixtureIsReadAsTheCorpusSays(t *testing.T) {
	e, read := corpus(t)
	entries, _ := os.ReadDir(filepath.Join(fixtures, "valid"))
	if len(entries) != len(e.Valid) {
		t.Fatalf("the corpus has %d valid streams and %d expectations", len(entries), len(e.Valid))
	}
	for name, want := range e.Valid {
		got, err := reading.Protocol2(read("valid", name), scoredIn[name])
		switch want.Outcome {
		case "error":
			var reported *reading.Reported
			if !errors.As(err, &reported) || reported.Message == "" {
				t.Errorf("%s: %v, want a reported error", name, err)
			}
		case "result":
			if err != nil {
				t.Errorf("%s: %v", name, err)
				continue
			}
			all := map[string]float64{scoredIn[name]: got.Value}
			for _, o := range got.Others {
				all[o.Name] = o.Value
			}
			if fmt.Sprint(all) != fmt.Sprint(want.Values) {
				t.Errorf("%s: read %v, want %v", name, all, want.Values)
			}
		default:
			t.Errorf("%s: outcome %q is not one this test knows", name, want.Outcome)
		}
	}
}

func TestAnErrorRecordCarriesItsRequirementAndEndsTheStream(t *testing.T) {
	hello := `{"kind":"hello","protocol":2,"metrics":{"m":{}}}` + "\n"
	for name, c := range map[string]struct {
		stream      string
		message     string
		requirement string
	}{
		"transient":       {hello + `{"kind":"error","message":"server down"}`, "server down", ""},
		"explicitly":      {hello + `{"kind":"error","message":"x","error_kind":"transient"}`, "x", ""},
		"not met":         {hello + `{"kind":"error","message":"no hits","error_kind":"requirement_not_met","requirement":"prefix_cache"}`, "no hits", "prefix_cache"},
		"with a partial":  {hello + `{"kind":"error","message":"stopped","partial":{"name":"warmup_rate","value":16.4,"direction":"max","unit":"tokens/s","target":79.7,"progress":{"completed":15,"required":72,"unit":"rounds"}}}`, "stopped", ""},
		"before a result": {hello + `{"kind":"error","message":"x"}` + "\n" + `{"kind":"result","values":{"m":1}}`, "x", ""},
		"before garbage":  {hello + `{"kind":"error","message":"x"}` + "\nthis is not json\n", "x", ""},
		"first, no hello": {`{"kind":"error","message":"config failed"}` + "\nnot json\n", "config failed", ""},
	} {
		_, err := reading.Protocol2([]byte(c.stream), "m")
		var reported *reading.Reported
		if !errors.As(err, &reported) {
			t.Errorf("%s: %v", name, err)
			continue
		}
		if reported.Message != c.message || reported.Requirement != c.requirement || !strings.Contains(reported.Error(), c.message) {
			t.Errorf("%s: %+v", name, reported)
		}
	}
}

func TestWhatTheProtocolForbidsIsRejectedAndNamed(t *testing.T) {
	hello := `{"kind":"hello","protocol":2,"metrics":{"m":{},"opt":{"required":false}}}` + "\n"
	result := `{"kind":"result","values":{"m":1}}` + "\n"
	for name, c := range map[string]struct {
		stream string
		scored string
		code   reading.Code
		names  string
	}{
		"nothing":                                  {"", "m", reading.MissingHello, ""},
		"blank lines only":                         {"\n  \n", "m", reading.MissingHello, ""},
		"the scored metric is optional":            {hello + `{"kind":"result","values":{"m":1,"opt":2}}`, "opt", reading.MissingMetric, "opt"},
		"the scored metric is undeclared":          {hello + result, "other", reading.MissingMetric, "other"},
		"a capitalized kind key":                   {`{"Kind":"hello","protocol":2,"metrics":{"m":{}}}`, "m", reading.UnknownKind, ""},
		"a capitalized field":                      {`{"kind":"hello","Protocol":2,"metrics":{"m":{}}}`, "m", reading.UnknownKey, "Protocol"},
		"a protocol written as a float":            {`{"kind":"hello","protocol":2.0,"metrics":{"m":{}}}`, "m", reading.InvalidRecord, "protocol"},
		"a missing protocol":                       {`{"kind":"hello","metrics":{"m":{}}}`, "m", reading.InvalidRecord, "protocol"},
		"a metric that is not an object":           {`{"kind":"hello","protocol":2,"metrics":{"m":1}}`, "m", reading.InvalidRecord, "m"},
		"an unknown key in a spec":                 {`{"kind":"hello","protocol":2,"metrics":{"m":{"color":"red"}}}`, "m", reading.UnknownKey, "metrics.m.color"},
		"a bad direction":                          {`{"kind":"hello","protocol":2,"metrics":{"m":{"direction":"up"}}}`, "m", reading.InvalidRecord, "direction"},
		"a null required":                          {`{"kind":"hello","protocol":2,"metrics":{"m":{"required":null}}}`, "m", reading.InvalidRecord, "required"},
		"a null label":                             {hello + `{"kind":"result","label":null,"values":{"m":1}}`, "m", reading.InvalidRecord, "label"},
		"values that are a list":                   {hello + `{"kind":"result","values":[1]}`, "m", reading.InvalidRecord, "values"},
		"a null value":                             {hello + `{"kind":"result","values":{"m":null}}`, "m", reading.NonNumericValue, "values.m"},
		"a string value":                           {hello + `{"kind":"result","values":{"m":"1"}}`, "m", reading.NonNumericValue, "values.m"},
		"a negative overflow":                      {hello + `{"kind":"result","values":{"m":-1e999}}`, "m", reading.NonFiniteValue, "values.m"},
		"a non-string kind":                        {`{"kind":3}`, "m", reading.UnknownKind, ""},
		"a bare number":                            {"3", "m", reading.MalformedLine, ""},
		"null":                                     {"null", "m", reading.MalformedLine, ""},
		"trailing junk":                            {`{"kind":"hello","protocol":2,"metrics":{"m":{}}} x`, "m", reading.MalformedLine, ""},
		"a hello after an error that is not first": {result + `{"kind":"error","message":"x"}` + "\n" + hello, "m", reading.HelloNotFirst, ""},
		"an error that is not first and no hello":  {result + `{"kind":"error","message":"x"}`, "m", reading.MissingHello, ""},
		"a requirement without its kind":           {hello + `{"kind":"error","message":"x","requirement":"r"}`, "m", reading.InvalidRecord, "requirement"},
		"a kind without its requirement":           {hello + `{"kind":"error","message":"x","error_kind":"requirement_not_met"}`, "m", reading.InvalidRecord, "requirement"},
		"an unknown error kind":                    {hello + `{"kind":"error","message":"x","error_kind":"fatal"}`, "m", reading.InvalidRecord, "error_kind"},
		"a partial with an unknown key":            {hello + `{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":"max","extra":1}}`, "m", reading.UnknownKey, "partial.extra"},
		"a partial value that overflows":           {hello + `{"kind":"error","message":"x","partial":{"name":"n","value":1e999,"direction":"max"}}`, "m", reading.InvalidRecord, "partial.value"},
		"a progress that completed a negative":     {hello + `{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":"max","progress":{"completed":-1,"required":3,"unit":"u"}}}`, "m", reading.InvalidRecord, "partial.progress.completed"},
		"a progress that requires nothing":         {hello + `{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":"max","progress":{"completed":0,"required":0,"unit":"u"}}}`, "m", reading.InvalidRecord, "partial.progress.required"},
		"a whitespace metric name in a result":     {hello + `{"kind":"result","values":{"m":1,"a b":2}}`, "m", reading.UnknownMetric, "a b"},
	} {
		_, err := reading.Protocol2([]byte(c.stream), c.scored)
		if err == nil {
			t.Errorf("%s: accepted", name)
			continue
		}
		r := rejection(t, err)
		if r.Code != c.code || !strings.Contains(r.Message, c.names) {
			t.Errorf("%s: %s %q, want %s naming %q", name, r.Code, r.Message, c.code, c.names)
		}
	}
}

func TestALineLongerThanTheLimitIsMalformed(t *testing.T) {
	long := `{"kind":"hello","protocol":2,"metrics":{"m":{"unit":"` + strings.Repeat("x", reading.MaxLineBytes) + `"}}}`
	_, err := reading.Protocol2([]byte(long), "m")
	if r := rejection(t, err); r.Code != reading.MalformedLine {
		t.Errorf("%s", r)
	}
}

// generated is a valid stream and what it holds. The hello line is rendered from its fields, so a
// mutation changes what the stream declares and not a string.
type generated struct {
	lines    []string
	declared map[string]bool   // name to required
	style    map[string]string // name to the unit and direction its spec writes
	explicit map[string]bool   // names whose spec writes "required":true
	values   map[string]float64
	scored   string
}

func name(r *rand.Rand) string {
	const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_./-%"
	n := 1 + r.IntN(12)
	b := make([]byte, n)
	for i := range b {
		b[i] = alphabet[r.IntN(len(alphabet))]
	}
	return string(b)
}

func number(r *rand.Rand) float64 {
	switch r.IntN(6) {
	case 0:
		return float64(r.IntN(2000) - 1000)
	case 1:
		return 0
	case 2:
		return r.NormFloat64() * 1e-9
	case 3:
		return r.NormFloat64() * 1e12
	case 4:
		return math.MaxFloat64 * (r.Float64() - 0.5)
	default:
		return r.NormFloat64()
	}
}

func token(v float64) string { return strconv.FormatFloat(v, 'g', -1, 64) }

func (g *generated) hello() string {
	var specs []string
	for _, n := range sortedKeys(g.declared) {
		var fields []string
		if g.style[n] != "" {
			fields = append(fields, g.style[n])
		}
		if !g.declared[n] {
			fields = append(fields, `"required":false`)
		} else if g.explicit[n] {
			fields = append(fields, `"required":true`)
		}
		specs = append(specs, fmt.Sprintf("%q:{%s}", n, strings.Join(fields, ",")))
	}
	return fmt.Sprintf(`{"kind":"hello","protocol":2,"metrics":{%s}}`, strings.Join(specs, ","))
}

func generate(r *rand.Rand) generated {
	g := generated{declared: map[string]bool{}, style: map[string]string{}, explicit: map[string]bool{}, values: map[string]float64{}}
	want := 1 + r.IntN(5)
	for len(g.declared) < want {
		n := name(r)
		if _, dup := g.declared[n]; dup {
			continue
		}
		g.declared[n] = r.IntN(4) != 0
		g.explicit[n] = r.IntN(4) == 0
		switch r.IntN(4) {
		case 0:
			g.style[n] = `"unit":"u"`
		case 1:
			g.style[n] = fmt.Sprintf(`"direction":%q`, []string{"max", "min"}[r.IntN(2)])
		case 2:
			g.style[n] = `"unit":"u","direction":"max"`
		}
	}
	var required []string
	for _, n := range sortedKeys(g.declared) {
		if g.declared[n] {
			required = append(required, n)
		}
	}
	if len(required) == 0 {
		n := sortedKeys(g.declared)[0]
		g.declared[n] = true
		required = append(required, n)
	}
	g.scored = required[r.IntN(len(required))]
	g.lines = append(g.lines, g.hello())

	var row []string
	for _, n := range sortedKeys(g.declared) {
		if g.declared[n] || r.IntN(2) == 0 {
			g.values[n] = number(r)
			row = append(row, fmt.Sprintf("%q:%s", n, token(g.values[n])))
		}
	}
	g.lines = append(g.lines, fmt.Sprintf(`{"kind":"result","label":"","values":{%s}}`, strings.Join(row, ",")))
	return g
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	slices.Sort(out)
	return out
}

// stream joins lines the way different producers do: LF or CRLF, blank lines between, a trailing
// newline or none.
func stream(r *rand.Rand, lines []string) []byte {
	eol := []string{"\n", "\r\n"}[r.IntN(2)]
	var b strings.Builder
	for i, l := range lines {
		for r.IntN(4) == 0 {
			b.WriteString([]string{"", "  ", "\t"}[r.IntN(3)] + eol)
		}
		b.WriteString(l)
		if i < len(lines)-1 || r.IntN(2) == 0 {
			b.WriteString(eol)
		}
	}
	return []byte(b.String())
}

func TestAnyValidStreamReadsBackExactlyWhatItHolds(t *testing.T) {
	for seed := uint64(0); seed < 500; seed++ {
		r := rand.New(rand.NewPCG(seed, 7))
		g := generate(r)
		got, err := reading.Protocol2(stream(r, g.lines), g.scored)
		if err != nil {
			t.Fatalf("seed %d: %v\n%s", seed, err, strings.Join(g.lines, "\n"))
		}
		if got.Value != g.values[g.scored] {
			t.Fatalf("seed %d: scored %v, want %v", seed, got.Value, g.values[g.scored])
		}
		want := map[string]float64{}
		for n, v := range g.values {
			if n != g.scored {
				want[n] = v
			}
		}
		if !slices.IsSortedFunc(got.Others, func(a, b reading.Named) int { return strings.Compare(a.Name, b.Name) }) {
			t.Fatalf("seed %d: others are not ordered by name: %v", seed, got.Others)
		}
		if len(got.Others) != len(want) {
			t.Fatalf("seed %d: others %v, want %v", seed, got.Others, want)
		}
		for _, o := range got.Others {
			if v, ok := want[o.Name]; !ok || v != o.Value {
				t.Fatalf("seed %d: other %s = %v, want %v", seed, o.Name, o.Value, v)
			}
		}
	}
}

// Every way to spoil a valid stream is rejected with the code that names the spoiling. The
// mutations are applied to generated streams, so the property is over the input space and not a
// few hand-picked streams.
func TestAnySpoiledStreamIsRejectedWithTheCodeForTheSpoiling(t *testing.T) {
	type mutation struct {
		name string
		code reading.Code
		do   func(r *rand.Rand, g *generated) bool
	}
	mutations := []mutation{
		{"drop a required value", reading.MissingMetric, func(r *rand.Rand, g *generated) bool {
			var row []string
			for n, v := range g.values {
				if n != g.scored {
					row = append(row, fmt.Sprintf("%q:%s", n, token(v)))
				}
			}
			slices.Sort(row)
			g.lines[1] = fmt.Sprintf(`{"kind":"result","values":{%s}}`, strings.Join(row, ","))
			return true
		}},
		{"report an undeclared metric", reading.UnknownMetric, func(r *rand.Rand, g *generated) bool {
			g.lines[1] = strings.Replace(g.lines[1], `"values":{`, `"values":{"zz undeclared":1,`, 1)
			return true
		}},
		{"report a boolean", reading.NonNumericValue, func(r *rand.Rand, g *generated) bool {
			g.lines[1] = strings.Replace(g.lines[1], fmt.Sprintf("%q:%s", g.scored, token(g.values[g.scored])), fmt.Sprintf("%q:true", g.scored), 1)
			return true
		}},
		{"report a string", reading.NonNumericValue, func(r *rand.Rand, g *generated) bool {
			g.lines[1] = strings.Replace(g.lines[1], fmt.Sprintf("%q:%s", g.scored, token(g.values[g.scored])), fmt.Sprintf(`%q:"%s"`, g.scored, token(g.values[g.scored])), 1)
			return true
		}},
		{"report an overflow", reading.NonFiniteValue, func(r *rand.Rand, g *generated) bool {
			g.lines[1] = strings.Replace(g.lines[1], fmt.Sprintf("%q:%s", g.scored, token(g.values[g.scored])), fmt.Sprintf("%q:1e999", g.scored), 1)
			return true
		}},
		{"label the row", reading.UnsupportedLabel, func(r *rand.Rand, g *generated) bool {
			g.lines[1] = strings.Replace(g.lines[1], `"label":""`, `"label":"x"`, 1)
			return true
		}},
		{"report two rows", reading.InvalidRecord, func(r *rand.Rand, g *generated) bool {
			g.lines = append(g.lines, g.lines[1])
			return true
		}},
		{"declare twice", reading.DuplicateHello, func(r *rand.Rand, g *generated) bool {
			g.lines = []string{g.lines[0], g.lines[0], g.lines[1]}
			return true
		}},
		{"put the row first", reading.HelloNotFirst, func(r *rand.Rand, g *generated) bool {
			g.lines[0], g.lines[1] = g.lines[1], g.lines[0]
			return true
		}},
		{"leave out the hello", reading.MissingHello, func(r *rand.Rand, g *generated) bool {
			g.lines = g.lines[1:]
			return true
		}},
		{"leave out the row", reading.NoOutcome, func(r *rand.Rand, g *generated) bool {
			g.lines = g.lines[:1]
			return true
		}},
		{"change the version", reading.UnsupportedProtocol, func(r *rand.Rand, g *generated) bool {
			g.lines[0] = strings.Replace(g.lines[0], `"protocol":2`, fmt.Sprintf(`"protocol":%d`, 3+r.IntN(5)), 1)
			return true
		}},
		{"add a key to the row", reading.UnknownKey, func(r *rand.Rand, g *generated) bool {
			g.lines[1] = strings.Replace(g.lines[1], `"label"`, `"elapsed_s":1,"label"`, 1)
			return true
		}},
		{"name an unknown kind", reading.UnknownKind, func(r *rand.Rand, g *generated) bool {
			g.lines = append(g.lines, `{"kind":"progress","done":1}`)
			return true
		}},
		{"write a line that is not an object", reading.MalformedLine, func(r *rand.Rand, g *generated) bool {
			g.lines = append(g.lines, "[1,2]")
			return true
		}},
		{"declare the scored metric optional", reading.MissingMetric, func(r *rand.Rand, g *generated) bool {
			g.declared[g.scored] = false
			g.lines[0] = g.hello()
			return true
		}},
	}
	for _, m := range mutations {
		ran := 0
		for seed := uint64(0); seed < 200; seed++ {
			r := rand.New(rand.NewPCG(seed, 11))
			g := generate(r)
			if !m.do(r, &g) {
				continue
			}
			ran++
			_, err := reading.Protocol2(stream(r, g.lines), g.scored)
			if err == nil {
				t.Fatalf("%s, seed %d: accepted\n%s", m.name, seed, strings.Join(g.lines, "\n"))
			}
			if got := rejection(t, err); got.Code != m.code {
				t.Fatalf("%s, seed %d: %s, want %s\n%s", m.name, seed, got, m.code, strings.Join(g.lines, "\n"))
			}
		}
		if ran == 0 {
			t.Errorf("%s: never applied", m.name)
		}
	}
}

func TestJSONFieldReadsTheNamedNumber(t *testing.T) {
	for name, c := range map[string]struct {
		file  string
		field string
		want  float64
		code  reading.Code
	}{
		"an object":              {`{"ops": 41250.3, "other": "x"}`, "ops", 41250.3, ""},
		"an integer":             {`{"ops": 7}`, "ops", 7, ""},
		"an exponent":            {"{\"ops\": 1.5e3}\n", "ops", 1500, ""},
		"a negative":             {`{"ops": -0.25}`, "ops", -0.25, ""},
		"one entry carries it":   {`[{"a": 1}, {"ops": 9}, 3, "x"]`, "ops", 9, ""},
		"only the top level":     {`{"nested": {"ops": 1}}`, "ops", 0, reading.MissingMetric},
		"no entry carries it":    {`[{"a": 1}]`, "ops", 0, reading.MissingMetric},
		"two entries carry it":   {`[{"ops": 1}, {"ops": 2}]`, "ops", 0, reading.AmbiguousMetric},
		"a boolean":              {`{"ops": true}`, "ops", 0, reading.NonNumericValue},
		"a string":               {`{"ops": "7"}`, "ops", 0, reading.NonNumericValue},
		"null":                   {`{"ops": null}`, "ops", 0, reading.NonNumericValue},
		"an overflow":            {`{"ops": 1e999}`, "ops", 0, reading.NonFiniteValue},
		"a case that differs":    {`{"Ops": 1}`, "ops", 0, reading.MissingMetric},
		"empty":                  {"  \n", "ops", 0, reading.MalformedFile},
		"not json":               {`ops=1`, "ops", 0, reading.MalformedFile},
		"a bare number":          {`3`, "ops", 0, reading.MalformedFile},
		"two values":             {`{"ops": 1} {"ops": 2}`, "ops", 0, reading.MalformedFile},
		"a truncated file":       {`{"ops": 1`, "ops", 0, reading.MalformedFile},
		"NaN, which is not JSON": {`{"ops": NaN}`, "ops", 0, reading.MalformedFile},
	} {
		got, err := reading.JSONField([]byte(c.file), c.field)
		if c.code == "" {
			if err != nil || got.Value != c.want || len(got.Others) != 0 {
				t.Errorf("%s: %+v, %v", name, got, err)
			}
			continue
		}
		if err == nil {
			t.Errorf("%s: accepted as %+v", name, got)
			continue
		}
		if r := rejection(t, err); r.Code != c.code {
			t.Errorf("%s: %s, want %s", name, r, c.code)
		}
	}
}

func TestJSONFieldReadsAnyObjectThatHoldsAFiniteNumber(t *testing.T) {
	for seed := uint64(0); seed < 500; seed++ {
		r := rand.New(rand.NewPCG(seed, 13))
		field, want := name(r), number(r)
		entry := func(carries bool) string {
			var fields []string
			for i := r.IntN(4); i > 0; i-- {
				if n := name(r); n != field {
					fields = append(fields, fmt.Sprintf("%q:%s", n, []string{"true", "null", `"s"`, "[1]", `{"x":1}`, token(number(r))}[r.IntN(6)]))
				}
			}
			if carries {
				fields = append(fields, fmt.Sprintf("%q:%s", field, token(want)))
			}
			r.Shuffle(len(fields), func(i, j int) { fields[i], fields[j] = fields[j], fields[i] })
			return "{" + strings.Join(fields, ",") + "}"
		}
		var file string
		if r.IntN(2) == 0 {
			file = entry(true)
		} else {
			entries := []string{entry(false), entry(true), entry(false)}
			r.Shuffle(len(entries), func(i, j int) { entries[i], entries[j] = entries[j], entries[i] })
			file = "[" + strings.Join(entries, ",") + "]"
		}
		got, err := reading.JSONField([]byte(file), field)
		if err != nil || got.Value != want {
			t.Fatalf("seed %d: %v, %v for\n%s", seed, got, err, file)
		}
	}
}

// What a fuzzer feeds the readers is the benchmark's own output, which is untrusted: the readers
// never panic, and what they accept is finite, ordered, and does not repeat the scored metric.
func checkOutcome(t *testing.T, got reading.Reading, err error, scored string) {
	t.Helper()
	if err != nil {
		var rej *reading.Rejection
		var rep *reading.Reported
		switch {
		case errors.As(err, &rej):
			if rej.Code == "" || rej.Message == "" {
				t.Fatalf("a rejection without a code or message: %+v", rej)
			}
		case errors.As(err, &rep):
			if rep.Message == "" {
				t.Fatalf("a reported error without a message: %+v", rep)
			}
		default:
			t.Fatalf("error %v is neither a rejection nor a reported error", err)
		}
		return
	}
	if math.IsInf(got.Value, 0) || math.IsNaN(got.Value) {
		t.Fatalf("accepted a non-finite value %v", got.Value)
	}
	for i, o := range got.Others {
		if math.IsInf(o.Value, 0) || math.IsNaN(o.Value) || o.Name == scored || o.Name == "" {
			t.Fatalf("accepted the other metric %+v", o)
		}
		if i > 0 && got.Others[i-1].Name >= o.Name {
			t.Fatalf("others are not strictly ordered by name: %v", got.Others)
		}
	}
}

func FuzzProtocol2(f *testing.F) {
	for _, kind := range []string{"valid", "invalid"} {
		entries, _ := os.ReadDir(filepath.Join(fixtures, kind))
		for _, e := range entries {
			data, err := os.ReadFile(filepath.Join(fixtures, kind, e.Name()))
			if err == nil {
				f.Add(data, "total_ops_per_sec")
				f.Add(data, "primary_value")
			}
		}
	}
	f.Add([]byte(`{"kind":"hello","protocol":2,"metrics":{"m":{}}}`+"\n"+`{"kind":"result","values":{"m":1}}`), "m")
	f.Fuzz(func(t *testing.T, stream []byte, scored string) {
		got, err := reading.Protocol2(stream, scored)
		checkOutcome(t, got, err, scored)
	})
}

func FuzzJSONField(f *testing.F) {
	f.Add([]byte(`{"ops": 1.5}`), "ops")
	f.Add([]byte(`[{"ops": 1}, {"x": 2}]`), "ops")
	f.Add([]byte(`{"ops": 1e999}`), "ops")
	f.Add([]byte(strings.Repeat("[", 100)), "ops")
	f.Fuzz(func(t *testing.T, file []byte, field string) {
		got, err := reading.JSONField(file, field)
		checkOutcome(t, got, err, field)
		if err == nil && len(got.Others) != 0 {
			t.Fatalf("a JSON file has no other metrics: %v", got.Others)
		}
	})
}
