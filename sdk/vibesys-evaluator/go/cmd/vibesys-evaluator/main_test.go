package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestVersionPrintsProtobufJSON(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"core", "version"}, strings.NewReader("{}"), &out, &errOut); code != exitOK {
		t.Fatalf("exit %d, stderr %q", code, errOut.String())
	}
	var got map[string]any
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatalf("stdout %q is not JSON: %v", out.String(), err)
	}
	if got["loadgen_version"] == "" || got["loadgen_version"] == nil {
		t.Fatalf("no loadgen_version in %v", got)
	}
}

func TestUnknownSubcommandIsACoreError(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"nope", "verb"}, strings.NewReader(""), &out, &errOut); code != exitCore {
		t.Fatalf("exit %d, want %d", code, exitCore)
	}
}

func TestDomainErrorExitsOneWithErrorKind(t *testing.T) {
	register("test", "fail", func(IO) error { return &DomainError{Kind: "bad_request", Message: "no"} })
	var out, errOut bytes.Buffer
	if code := run([]string{"test", "fail"}, strings.NewReader(""), &out, &errOut); code != exitDomain {
		t.Fatalf("exit %d, want %d", code, exitDomain)
	}
	var got map[string]string
	if err := json.Unmarshal(out.Bytes(), &got); err != nil || got["error_kind"] != "bad_request" || got["message"] != "no" {
		t.Fatalf("stdout %q (%v)", out.String(), err)
	}
}
