package archcheck_test

import (
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/archcheck"
)

func TestWatchRunsReadersUseDecoder(t *testing.T) {
	const name = "orchestrator/internal/client/reader_test.go"
	for _, method := range []string{"GetEvent", "DecodeWatchRuns"} {
		source := "package client\nfunc read() { x." + method + "() }"
		got := archcheck.Violations(map[string]string{name: source})
		if (len(got) > 0) != (method == "GetEvent") {
			t.Fatalf("%s: %v", method, got)
		}
		if len(got) > 0 && !strings.Contains(got[0], name+":2:") {
			t.Fatal(got)
		}
	}
}
