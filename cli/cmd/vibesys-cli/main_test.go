package main

import (
	"bytes"
	"slices"
	"testing"
	"testing/quick"
)

func TestVersionNamesTheBinaryAndExitsZero(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run([]string{"--version"}, &stdout, &stderr)
	if code != exitOK {
		t.Fatalf("exit code = %d, want %d", code, exitOK)
	}
	if got, want := stdout.String(), "vibesys-cli "+version+"\n"; got != want {
		t.Fatalf("stdout = %q, want %q", got, want)
	}
	if stderr.Len() != 0 {
		t.Fatalf("stderr = %q, want empty", stderr.String())
	}
}

// Every other invocation (upgrade has its own tests) is invalid input for now: nothing is written to stdout, where a caller
// reads results, and the exit code is the one the contract reserves for invalid input.
func TestAnyOtherInvocationIsInvalidInput(t *testing.T) {
	property := func(args []string) bool {
		if slices.Equal(args, []string{"--version"}) || (len(args) > 0 && args[0] == "upgrade") {
			return true
		}
		var stdout, stderr bytes.Buffer
		code := run(args, &stdout, &stderr)
		return code == exitInvalidInput && stdout.Len() == 0 && stderr.Len() > 0
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}
