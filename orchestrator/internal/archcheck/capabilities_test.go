package archcheck_test

import (
	"fmt"
	"github.com/uw-syfi/vibesys/orchestrator/internal/archcheck"
	"strings"
	"testing"
)

func TestCapabilityRules(t *testing.T) {
	for _, tc := range []struct {
		file, source, rule string
		bad                bool
	}{
		{"internal/wire/a.go", goFile("github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"), "cli-contract-importers", true},
		{"internal/clidaemon/a.go", goFile("github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"), "cli-contract-importers", false},
		{"internal/clidaemon/clidaemontest/a.go", "package p\nimport x \"github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1\"\nvar _ x.DaemonServiceClient", "cli-stub", true},
		{"internal/clidaemon/a.go", "package p\nimport x \"github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1\"\nvar _ x.DaemonServiceClient", "cli-stub", false},
		{"internal/production/a.go", goFile("os/exec"), "exec-confined", true},
		{"internal/production/production.go", goFile("os/exec"), "exec-confined", true},
		{"internal/production/cli_ensure.go", goFile("os/exec"), "exec-confined", false},
		{"internal/production/cli_transport.go", "package p\nimport \"os\"\nvar read = os.Lstat", "fs-reads", false},
		{"internal/search/a.go", "package p\nimport \"os\"\nvar read = os.ReadFile", "fs-reads", true},
		{"internal/wire/a.go", "package p\nimport x \"os\"\nvar read = x.ReadFile", "fs-reads", true},
		{"internal/wire/a.go", goFile("os"), "fs-reads", false},
		{"internal/wire/a.go", "package p\nimport x \"path/filepath\"\nvar walk = x.WalkDir", "fs-reads", true},
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
