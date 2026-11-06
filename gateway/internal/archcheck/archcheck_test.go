package archcheck_test

import (
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/gateway/internal/archcheck"
)

const module = "github.com/uw-syfi/vibesys/"

func goFile(imports ...string) string {
	var b strings.Builder
	b.WriteString("package p\nimport (\n")
	for _, target := range imports {
		b.WriteString("_ \"" + target + "\"\n")
	}
	b.WriteString(")\n")
	return b.String()
}

func tree() map[string]string {
	return map[string]string{
		archcheck.CommandDirectory + "/main.go": goFile("os", module+"gateway/internal/hostmain"),
		archcheck.ClientDirectory + "/main.go":  goFile("os", module+"gateway/internal/clientmain"),
	}
}

func TestRules(t *testing.T) {
	for _, target := range []string{
		module + "cli/gen/vibesys/cli/v1", module + "gateway/gen/vibesys/api/v1",
		module + "gateway/internal/runlog", module + "gateway/internal/runindex",
		module + "gateway/internal/hostoperations", module + "gateway/internal/machinestats",
		module + "gateway/internal/subscription", module + "gateway/internal/hostcommand",
		module + "other/hosted", module + "orchestrator/hosted/extra", "example.com/hosted", "C",
	} {
		t.Run(target, func(t *testing.T) {
			files := tree()
			name := "gateway/internal/hostmain/a.go"
			files[name] = goFile(target)
			got := archcheck.Violations(files)
			if len(got) != 1 || !strings.Contains(got[0], name) || !strings.Contains(got[0], target) {
				t.Fatalf("violations = %v", got)
			}
		})
	}
	for _, tc := range []struct {
		name, file, source string
		bad                bool
	}{
		{"allowed composition", "gateway/internal/hostmain/a.go", goFile("context", "os/signal", module+"gateway/internal/connect", module+"gateway/internal/lifecycle", module+"gateway/internal/pipe", module+"gateway/internal/server", module+"gateway/schemahash", module+"orchestrator/hosted"), false},
		{"client composition", "gateway/internal/clientmain/a.go", goFile("context", "github.com/mattn/go-isatty", module+"gateway/internal/connect", module+"gateway/internal/lifecycle", module+"gateway/schemahash", module+"orchestrator/clientcli", module+"orchestrator/gatewayroot"), false},
		{"client may not import the host's orchestrator", "gateway/internal/clientmain/a.go", goFile(module + "orchestrator/hosted"), true},
		{"host may not import the client's verbs", "gateway/internal/hostmain/a.go", goFile(module + "orchestrator/clientcli"), true},
		{"second client command file", archcheck.ClientDirectory + "/extra.go", goFile("os", module+"gateway/internal/clientmain"), true},
		{"client command with the wrong composition", archcheck.ClientDirectory + "/main.go", goFile("os", module+"gateway/internal/hostmain"), true},
		{"command tests allowed", archcheck.CommandDirectory + "/main_test.go", goFile(module + "gateway/gen/vibesys/api/v1"), false},
		{"hostmain tests allowed", "gateway/internal/hostmain/a_test.go", goFile(module + "cli/internal/core"), false},
		{"second command file", archcheck.CommandDirectory + "/extra.go", goFile("os", module+"gateway/internal/hostmain"), true},
		{"extra command import", archcheck.CommandDirectory + "/main.go", goFile("os", module+"gateway/internal/hostmain", "fmt"), true},
		{"missing command import", archcheck.CommandDirectory + "/main.go", goFile("os"), true},
		{"malformed source", "gateway/internal/hostmain/a.go", "package (", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			files := tree()
			files[tc.file] = tc.source
			got := archcheck.Violations(files)
			if (len(got) > 0) != tc.bad {
				t.Fatalf("violations = %v, want bad %v", got, tc.bad)
			}
		})
	}
	if got := archcheck.Violations(nil); len(got) == 0 {
		t.Fatal("missing command accepted")
	}
}

// The client must stay a thin program: linking the daemon or the store into it would make every
// client a second host.
func TestTheClientBinaryLinksNeitherTheDaemonNorTheStore(t *testing.T) {
	out, err := exec.Command("go", "list", "-deps", "-f", "{{.ImportPath}}", "../../cmd/vibesys-client").Output()
	if err != nil {
		t.Fatalf("go list: %v", err)
	}
	for _, dep := range strings.Fields(string(out)) {
		for _, banned := range []string{"orchestrator/internal/daemon", "orchestrator/internal/store", "orchestrator/internal/production", "orchestrator/hosted", "modernc.org/sqlite"} {
			if strings.Contains(dep, banned) {
				t.Errorf("vibesys-client links %s", dep)
			}
		}
	}
	if !strings.Contains(string(out), "orchestrator/clientcli") {
		t.Errorf("go list did not see the client's verbs:\n%s", out)
	}
}

func TestRepositoryObeysTheRules(t *testing.T) {
	root := "../.."
	files := map[string]string{}
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || filepath.Ext(p) != ".go" {
			return nil
		}
		source, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		files["gateway/"+filepath.ToSlash(rel)] = string(source)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, v := range archcheck.Violations(files) {
		t.Error(v)
	}
}
