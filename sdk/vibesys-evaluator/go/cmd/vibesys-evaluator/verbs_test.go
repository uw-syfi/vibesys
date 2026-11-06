package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func call(t *testing.T, group, name, request string) (int, map[string]any) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	code := run([]string{group, name}, strings.NewReader(request), &stdout, &stderr)
	var out map[string]any
	if stdout.Len() > 0 {
		if err := json.Unmarshal(stdout.Bytes(), &out); err != nil {
			t.Fatalf("%s %s: response %q: %v", group, name, stdout.String(), err)
		}
	}
	return code, out
}

func TestTheRecordsOfASampleAreWrittenOneCallAtATime(t *testing.T) {
	path := filepath.Join(t.TempDir(), "r.jsonl")
	q := func(rec string) string { return `{"path":` + jsonQuote(path) + `,` + rec + `}` }
	steps := []struct {
		req  string
		code int
		want string
	}{
		{q(`"fresh":true,"hello":{"metrics":[{"name":"ops","unit":"ops/s","direction":"DIRECTION_MAX"}]}`), 0, ""},
		{q(`"hello":{"metrics":[{"name":"ops","direction":"DIRECTION_MAX"}]}`), 1, "record"},
		{q(`"result":{"values":[{"name":"ops","value":5300}]}`), 0, "result"},
		{q(`"result":{"values":[{"name":"ops","value":1}]}`), 1, "record"},
		{q(`"error":{"message":"teardown","partial":{"name":"ops","value":3,"direction":"DIRECTION_MAX"}}`), 0, "error"},
	}
	for i, s := range steps {
		code, out := call(t, "records", "write", s.req)
		if code != s.code {
			t.Fatalf("step %d: exit %d, response %v", i, code, out)
		}
		if code == 0 && out["outcome"] != s.want || code == 1 && out["error_kind"] != s.want {
			t.Errorf("step %d: %v, want %q", i, out, s.want)
		}
	}
	got, _ := os.ReadFile(path)
	want := `{"kind":"hello","protocol":2,"metrics":{"ops":{"unit":"ops/s","direction":"max"}}}
{"kind":"result","label":"","values":{"ops":5300}}
{"kind":"error","message":"teardown","partial":{"name":"ops","value":3,"direction":"max"}}
`
	if string(got) != want {
		t.Errorf("file:\n%s", got)
	}
}

func jsonQuote(s string) string { b, _ := json.Marshal(s); return string(b) }

func TestASampleStartsAndHasPhases(t *testing.T) {
	dir := t.TempDir()
	code, out := call(t, "sample", "start", `{"mode":"benchmark","seed_flag":"42","scratch_dir":`+jsonQuote(dir)+`}`)
	if code != 0 || out["seed"] != "42" || out["seed_source"] != "--seed" {
		t.Fatalf("start: %d %v", code, out)
	}
	artifacts := out["artifacts_dir"].(string)
	if code, out := call(t, "sample", "phase", `{"artifacts_dir":`+jsonQuote(artifacts)+`,"name":"phase-0"}`); code != 0 {
		t.Fatalf("phase: %d %v", code, out)
	}
	code, out = call(t, "sample", "phase", `{"artifacts_dir":`+jsonQuote(artifacts)+`,"name":"phase-0"}`)
	if code != 1 || !strings.Contains(out["message"].(string), "used twice") {
		t.Errorf("a second phase of one name: %d %v", code, out)
	}
	if code, _ := call(t, "sample", "start", `{"mode":"nap"}`); code != 1 {
		t.Error("an unknown mode is a domain error")
	}
}

func TestTrialsCombine(t *testing.T) {
	code, out := call(t, "sample", "combine", `{"series":[{"name":"ops","aggregation":"AGGREGATION_MEDIAN","values":[3,1,2]},{"name":"p99","aggregation":"AGGREGATION_MAX","values":[7,9]}]}`)
	values := out["values"].([]any)
	if code != 0 || values[0].(map[string]any)["value"] != 2.0 || values[1].(map[string]any)["value"] != 9.0 {
		t.Fatalf("%d %v", code, out)
	}
	if code, _ := call(t, "sample", "combine", `{"series":[{"name":"x","aggregation":"AGGREGATION_MEAN"}]}`); code != 1 {
		t.Error("no trials is a domain error")
	}
}

func TestHistogramsMergeAndSummarise(t *testing.T) {
	code, out := call(t, "hist", "summary", `{"percentiles":[50,99]}`)
	if code != 0 || out["count"] != "0" {
		t.Fatalf("an empty merge: %d %v", code, out)
	}
	if code, out := call(t, "hist", "summary", `{"histograms":["AAAA"]}`); code != 1 || out["error_kind"] != "histogram" {
		t.Errorf("garbage: %d %v", code, out)
	}
}

func TestAMalformedRequestIsADomainError(t *testing.T) {
	if code, _ := call(t, "records", "write", `{not json`); code != 1 {
		t.Error("a malformed request is a domain error")
	}
}
