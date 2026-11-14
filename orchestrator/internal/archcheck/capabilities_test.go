package archcheck_test

import (
	"fmt"
	"github.com/uw-syfi/vibesys/orchestrator/internal/archcheck"
	"os"
	"strings"
	"testing"
)

func TestCapabilityRules(t *testing.T) {
	for _, tc := range []struct {
		file, source, rule string
		bad                bool
	}{
		{"internal/wire/a.go", goFile("github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"), "cli-contract-importers", true},
		{"internal/wire/setup_facts.go", goFile("github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"), "cli-contract-importers", false},
		{"internal/daemon/run_search.go", goFile("github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"), "cli-contract-importers", false},
		{"internal/daemon/a.go", goFile("github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"), "cli-contract-importers", true},
		{"internal/clidaemon/a.go", goFile("github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"), "cli-contract-importers", false},
		{"internal/clidaemon/clidaemontest/a.go", "package p\nimport x \"github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1\"\nvar _ x.DaemonServiceClient", "cli-stub", true},
		{"internal/clidaemon/a.go", "package p\nimport x \"github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1\"\nvar _ x.DaemonServiceClient", "cli-stub", false},
		{"internal/production/a.go", goFile("os/exec"), "exec-confined", true},
		{"internal/production/production.go", goFile("os/exec"), "exec-confined", true},
		{"internal/production/cli_ensure.go", goFile("os/exec"), "exec-confined", false},
		{"internal/production/cli_transport.go", "package p\nimport \"os\"\nvar read = os.Lstat", "fs-reads", true},
		{"internal/search/a.go", "package p\nimport \"os\"\nvar read = os.ReadFile", "fs-reads", true},
		{"internal/wire/a.go", "package p\nimport x \"os\"\nvar read = x.ReadFile", "fs-reads", true},
		{"internal/wire/a.go", goFile("os"), "fs-reads", false},
		{"internal/wire/a.go", "package p\nimport x \"path/filepath\"\nvar walk = x.WalkDir", "fs-reads", true},
		{"internal/wire/a.go", "package p\nimport \"os\"\nvar w = os.WriteFile", "fs-writes", true},
		{"internal/store/sqlite.go", "package p\nimport \"os\"\nvar w = os.MkdirAll", "fs-writes", false},
		{"internal/wire/a.go", "package p\nimport \"os\"\nvar e = os.Getenv", "env-reads", true},
		{"internal/wire/a.go", "package p\nimport \"os\"\nvar e = os.Environ", "env-reads", true},
		{"hosted/command.go", "package p\nimport \"os\"\nvar e = os.Getenv", "env-reads", false},
		{"internal/wire/a.go", "package p\nimport \"encoding/json\"\nvar j = json.Marshal", "json-use", true},
		{"internal/wire/a.go", goFile("encoding/json"), "json-use", false},
		{"internal/wire/a.go", "package p\nimport . \"encoding/json\"\nvar j = Marshal", "capability-import", true},
		{"internal/wire/a.go", "package p\nimport \"os\"\nvar a = os.Args", "args-use", true},
		{"internal/production/cli_ensure.go", "package p\nimport \"os/exec\"\nvar c = exec.Command", "exec-calls", false},
		{"internal/production/cli_ensure.go", "package p\nimport \"os/exec\"\nvar c = exec.Command\nvar d = exec.CommandContext", "exec-calls", true},
	} {
		t.Run(tc.rule+tc.file+fmt.Sprint(tc.bad), func(t *testing.T) {
			path := "orchestrator/" + tc.file
			found := false
			for _, v := range archcheck.Violations(map[string]string{path: tc.source}) {
				if strings.Contains(v, tc.rule) {
					found = true
					if !strings.Contains(v, path) {
						t.Fatal(v)
					}
				}
			}
			if found != tc.bad {
				t.Fatalf("%s: found=%v want=%v", tc.rule, found, tc.bad)
			}
		})
	}
}

// A violation says which rule broke, where, and what to do; the fix names the design doc.
func TestABoundaryViolationNamesThePathTheRuleAndTheFix(t *testing.T) {
	path := "orchestrator/internal/wire/a.go"
	got := archcheck.Violations(map[string]string{path: "package p\nimport \"os\"\nvar e = os.Getenv"})
	if len(got) != 1 || !strings.Contains(got[0], path+": env-reads") || !strings.Contains(got[0], "docs/contributing/process-boundaries.md") {
		t.Fatalf("violations = %v", got)
	}
}

// The baseline is shrink-only: an entry that guards nothing is itself a violation.
func TestTheBaselineHoldsNoEntryForAFileThatIsGoneOrCrossesNothing(t *testing.T) {
	if stale := archcheck.StaleBaselines(func(string) bool { return false }); len(stale) == 0 {
		t.Fatal("a baseline whose files are gone passed")
	}
	for _, s := range archcheck.StaleBaselines(func(rel string) bool {
		_, err := os.Stat("../../" + rel)
		return err == nil
	}) {
		t.Error(s)
	}
}

func TestFileReadBaselineCannotGrowOrLeaveSlack(t *testing.T) {
	// Generate aliases and counts. Neither renaming os nor taking a function
	// value may escape the per-file capability count.
	for alias := 0; alias < 20; alias++ {
		for count := 0; count < 5; count++ {
			name := "orchestrator/internal/production/holder.go"
			src := fmt.Sprintf("package p\nimport alias%d \"os\"\n", alias)
			for n := 0; n < count; n++ {
				src += fmt.Sprintf("var read%d = alias%d.ReadFile\n", n, alias)
			}
			got := strings.Join(archcheck.Violations(map[string]string{name: src}), "\n")
			if strings.Contains(got, "fs-reads") != (count != 1) {
				t.Fatalf("alias %d count %d: %s", alias, count, got)
			}
		}
	}
}
