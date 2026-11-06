package evaluator

import (
	"encoding/base64"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"testing"
)

// The cases of fixtures/core.json were written from the Python implementation this package
// replaced; every one must come out the same here, byte for byte where a file is written.

type fixture struct {
	Percentiles []float64 `json:"percentiles"`
	Histograms  []struct {
		Name        string  `json:"name"`
		Encoded     string  `json:"encoded"`
		Count       int64   `json:"count"`
		Max         int64   `json:"max"`
		Percentiles []int64 `json:"percentiles"`
	} `json:"histograms"`
	Merges []struct {
		Name        string   `json:"name"`
		Encoded     []string `json:"encoded"`
		Count       int64    `json:"count"`
		Max         int64    `json:"max"`
		Percentiles []int64  `json:"percentiles"`
	} `json:"merges"`
	Records []struct {
		Name string `json:"name"`
		Ops  []struct {
			Op      string `json:"op"`
			Metrics []struct {
				Name      string `json:"name"`
				Unit      string `json:"unit"`
				Direction string `json:"direction"`
			} `json:"metrics"`
			Values  [][2]any `json:"values"`
			Message string   `json:"message"`
			Partial *struct {
				Name      string `json:"name"`
				Value     any    `json:"value"`
				Direction string `json:"direction"`
				Unit      string `json:"unit"`
			} `json:"partial"`
		} `json:"ops"`
		Expect struct {
			File   string    `json:"file"`
			Errors []*string `json:"errors"`
		} `json:"expect"`
	} `json:"records"`
	Validity []struct {
		MaxErrorRate      any     `json:"max_error_rate"`
		MinSustainedRatio any     `json:"min_sustained_ratio"`
		MinOps            int     `json:"min_ops_per_operation"`
		Error             *string `json:"error"`
	} `json:"validity"`
	Metrics []struct {
		Direction string  `json:"direction"`
		Error     *string `json:"error"`
	} `json:"metrics"`
	Seeds []struct {
		Flag   *string `json:"flag"`
		Env    string  `json:"env"`
		Expect struct {
			Seed   string `json:"seed"`
			Source string `json:"source"`
			Error  string `json:"error"`
		} `json:"expect"`
	} `json:"seeds"`
	Combine []struct {
		Aggregation string    `json:"aggregation"`
		Trials      []float64 `json:"trials"`
		Want        float64   `json:"want"`
	} `json:"combine"`
}

func loadFixture(t *testing.T) fixture {
	t.Helper()
	data, err := os.ReadFile("../../fixtures/core.json")
	if err != nil {
		t.Fatal(err)
	}
	var f fixture
	if err := json.Unmarshal(data, &f); err != nil {
		t.Fatal(err)
	}
	return f
}

func number(t *testing.T, v any) float64 {
	t.Helper()
	switch v := v.(type) {
	case float64:
		return v
	case string:
		switch v {
		case "NaN":
			return math.NaN()
		case "Infinity":
			return math.Inf(1)
		case "-Infinity":
			return math.Inf(-1)
		}
	}
	t.Fatalf("not a number: %v", v)
	return 0
}

func checkSummary(t *testing.T, name string, h *Histogram, percentiles []float64, count, maxv int64, want []int64) {
	t.Helper()
	if h.Total() != count || h.Max() != maxv {
		t.Errorf("%s: count %d max %d, want %d %d", name, h.Total(), h.Max(), count, maxv)
	}
	for i, p := range percentiles {
		if got := h.Percentile(p); got != want[i] {
			t.Errorf("%s: p%v = %d, want %d", name, p, got, want[i])
		}
	}
}

func TestHistogramsEqualPythons(t *testing.T) {
	f := loadFixture(t)
	for _, c := range f.Histograms {
		raw, _ := base64.StdEncoding.DecodeString(c.Encoded)
		h, err := DecodeHistogram(raw)
		if err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		checkSummary(t, c.Name, h, f.Percentiles, c.Count, c.Max, c.Percentiles)
		// The re-encoding is what Python's encoder wrote (zlib levels may differ in bytes, so
		// compare what decodes).
		again, err := DecodeHistogram(h.Encode())
		if err != nil {
			t.Fatalf("%s: re-decode: %v", c.Name, err)
		}
		checkSummary(t, c.Name+" (re-encoded)", again, f.Percentiles, c.Count, c.Max, c.Percentiles)
		// The base64 text form decodes too.
		if _, err := DecodeHistogram([]byte(base64.StdEncoding.EncodeToString(h.Encode()))); err != nil {
			t.Fatalf("%s: base64 form: %v", c.Name, err)
		}
	}
}

func TestMergedHistogramsEqualPythons(t *testing.T) {
	f := loadFixture(t)
	for _, c := range f.Merges {
		var encoded [][]byte
		for _, e := range c.Encoded {
			raw, _ := base64.StdEncoding.DecodeString(e)
			encoded = append(encoded, raw)
		}
		h, err := MergeHistograms(encoded...)
		if err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		checkSummary(t, c.Name, h, f.Percentiles, c.Count, c.Max, c.Percentiles)
	}
}

func TestGarbageHistogramsAreRefused(t *testing.T) {
	for name, data := range map[string][]byte{
		"empty": {}, "bad-cookie": make([]byte, 8), "bad-base64": []byte("HISTF!!!"),
		"bad-zlib": {0x1c, 0x84, 0x93, 0x04, 0, 0, 0, 2, 'x', 'x'},
	} {
		if _, err := DecodeHistogram(data); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestRecordsEqualPythons(t *testing.T) {
	f := loadFixture(t)
	for _, c := range f.Records {
		path := filepath.Join(t.TempDir(), "out.jsonl")
		s := NewStream(path)
		for n, op := range c.Ops {
			var err error
			switch op.Op {
			case "hello":
				var ms []NamedMetric
				for _, m := range op.Metrics {
					ms = append(ms, NamedMetric{Name: m.Name, Metric: Metric{Unit: m.Unit, Direction: Direction(m.Direction)}})
				}
				err = s.Hello(ms)
			case "result":
				values := map[string]float64{}
				for _, kv := range op.Values {
					values[kv[0].(string)] = number(t, kv[1])
				}
				err = s.Result(values)
			default:
				var p *Partial
				if op.Partial != nil {
					p = &Partial{Name: op.Partial.Name, Value: number(t, op.Partial.Value), Direction: Direction(op.Partial.Direction), Unit: op.Partial.Unit}
				}
				err = s.Error(op.Message, p)
			}
			want := c.Expect.Errors[n]
			switch {
			case want == nil && err != nil:
				t.Errorf("%s op %d: %v", c.Name, n, err)
			case want != nil && err == nil:
				t.Errorf("%s op %d: no error, want %q", c.Name, n, *want)
			case want != nil && err.Error() != *want:
				t.Errorf("%s op %d: error %q, want %q", c.Name, n, err, *want)
			}
		}
		s.Close()
		got, _ := os.ReadFile(path)
		if string(got) != c.Expect.File {
			t.Errorf("%s: file\n%s\nwant\n%s", c.Name, got, c.Expect.File)
		}
	}
}

func TestValidityAndMetricRulesEqualPythons(t *testing.T) {
	f := loadFixture(t)
	for _, c := range f.Validity {
		err := Validity{MaxErrorRate: number(t, c.MaxErrorRate), MinSustainedRatio: number(t, c.MinSustainedRatio), MinOpsPerOperation: c.MinOps}.Validate()
		compareError(t, "validity", err, c.Error)
	}
	for _, c := range f.Metrics {
		compareError(t, "metric "+c.Direction, Metric{Unit: "u", Direction: Direction(c.Direction)}.Validate(), c.Error)
	}
}

func compareError(t *testing.T, what string, err error, want *string) {
	t.Helper()
	switch {
	case want == nil && err != nil:
		t.Errorf("%s: %v", what, err)
	case want != nil && err == nil:
		t.Errorf("%s: no error, want %q", what, *want)
	case want != nil && err.Error() != *want:
		t.Errorf("%s: error %q, want %q", what, err, *want)
	}
}

func TestSampleSeedsEqualPythons(t *testing.T) {
	f := loadFixture(t)
	for _, c := range f.Seeds {
		flag := ""
		if c.Flag != nil {
			flag = *c.Flag
		}
		seed, source, err := SampleSeed(flag, c.Flag != nil, c.Env)
		name := "flag=" + flag + " env=" + c.Env
		switch {
		case c.Expect.Error != "":
			if err == nil || err.Error() != c.Expect.Error {
				t.Errorf("%s: error %v, want %q", name, err, c.Expect.Error)
			}
		case err != nil:
			t.Errorf("%s: %v", name, err)
		case c.Expect.Source == "drawn":
			if source != "drawn" || seed >= 1<<63 {
				t.Errorf("%s: %d from %s", name, seed, source)
			}
		case strconv.FormatUint(seed, 10) != c.Expect.Seed || source != c.Expect.Source:
			t.Errorf("%s: %d from %s, want %s from %s", name, seed, source, c.Expect.Seed, c.Expect.Source)
		}
	}
}

func TestCombineEqualsPythons(t *testing.T) {
	f := loadFixture(t)
	for _, c := range f.Combine {
		got, err := Combine(Aggregation(c.Aggregation), c.Trials)
		if err != nil || got != c.Want {
			t.Errorf("%s %v = %v (%v), want %v", c.Aggregation, c.Trials, got, err, c.Want)
		}
	}
}
