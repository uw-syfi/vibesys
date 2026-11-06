package archcheck_test

import (
	"os"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/gateway/internal/archcheck"
)

func TestBoundaryRules(t *testing.T) {
	for _, tc := range []struct {
		name, file, source, rule string
		bad                      bool
	}{
		{"new file read", "gateway/internal/pipe/a.go", "package p\nimport \"os\"\nvar r = os.ReadFile", "fs-reads", true},
		{"new file write", "gateway/internal/pipe/a.go", "package p\nimport \"os\"\nvar w = os.WriteFile", "fs-writes", true},
		{"new environment read", "gateway/internal/pipe/a.go", "package p\nimport \"os\"\nvar e = os.LookupEnv", "env-reads", true},
		{"new hand JSON", "gateway/internal/pipe/a.go", "package p\nimport \"encoding/json\"\nvar j = json.Marshal", "json-use", true},
		{"new command start", "gateway/internal/pipe/a.go", "package p\nimport \"os/exec\"\nvar c = exec.Command", "exec-calls", true},
		{"new argument vector", "gateway/internal/pipe/a.go", "package p\nimport \"os\"\nvar a = os.Args", "args-use", true},
		{"renamed import is still counted", "gateway/internal/pipe/a.go", "package p\nimport x \"os\"\nvar r = x.ReadFile", "fs-reads", true},
		{"dot import hides the use", "gateway/internal/pipe/a.go", "package p\nimport . \"os\"\nvar r = ReadFile", "capability-import", true},
		{"clean file", "gateway/internal/pipe/a.go", goFile("os"), "fs-reads", false},
		{"listed file at its baseline", "gateway/cmd/vibesys-host/main.go", "package p\nimport \"os\"\nvar a = os.Args", "args-use", false},
		{"listed file past its baseline", "gateway/cmd/vibesys-host/main.go", "package p\nimport \"os\"\nvar a, b = os.Args, os.Args", "args-use", true},
		{"tests are not boundaries", "gateway/internal/pipe/a_test.go", "package p\nimport \"os\"\nvar r = os.ReadFile", "fs-reads", false},
		{"generated code is not a boundary", "gateway/gen/a.go", "package p\nimport \"os\"\nvar r = os.ReadFile", "fs-reads", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			files := map[string]string{}
			files[tc.file] = tc.source
			found := false
			for _, v := range archcheck.BoundaryViolations(files) {
				if strings.Contains(v, tc.file+": "+tc.rule) {
					found = true
					if !strings.Contains(v, "docs/contributing/process-boundaries.md") && tc.rule != "capability-import" {
						t.Errorf("the message does not name the fix: %s", v)
					}
				}
			}
			if found != tc.bad {
				t.Fatalf("%s: found=%v want=%v", tc.rule, found, tc.bad)
			}
		})
	}
}

// The baseline is shrink-only: an entry that guards nothing is itself a violation.
func TestTheBaselineHoldsNoEntryForAFileThatIsGoneOrCrossesNothing(t *testing.T) {
	if stale := archcheck.StaleBaselines(func(string) bool { return false }); len(stale) == 0 {
		t.Fatal("a baseline whose files are gone passed")
	}
	for _, s := range archcheck.StaleBaselines(func(name string) bool {
		_, err := os.Stat("../../../" + name)
		return err == nil
	}) {
		t.Error(s)
	}
}
