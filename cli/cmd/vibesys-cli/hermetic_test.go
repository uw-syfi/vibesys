package main

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/production"
)

// operatorPrefixes are the prefixes of every environment name the tool reads, through its one getenv
// and otherwise, but HOME: the context flags' defaults, the sandbox switch, the operator config, the
// runtime, state, config and cache directories (see the README's list and the identity golden). A
// developer's shell that runs a live daemon exports them, and a test that starts the tool, its daemon
// or a supervisor from os.Environ would reach that daemon's socket, its index or the operator's
// config. A whole prefix is taken, so a name the tool starts to read later is covered without being
// listed here. HOME stays: git and docker read it, and every test that starts the tool gives it a
// HOME of its own.
var operatorPrefixes = []string{"VIBESYS_", "XDG_"}

// scrubOperatorEnvironment removes from this process's environment every name of operatorPrefixes, so
// the tests, and every process they start from os.Environ, begin from none of them and set only the
// ones they mean to. TestMain calls it before the tests run, and only then: a process started as the
// daemon, a supervisor or the tool is given its environment by the test on purpose.
func scrubOperatorEnvironment() {
	for _, kv := range os.Environ() {
		name, _, _ := strings.Cut(kv, "=")
		for _, prefix := range operatorPrefixes {
			if strings.HasPrefix(name, prefix) {
				os.Unsetenv(name)
			}
		}
	}
}

// A test run from a shell that exports a live daemon's environment neither fails for it nor touches
// that daemon's places: the test binary, started with the operator's names set as such a shell sets
// them, runs a test of the real tool, which passes, and nothing is written where those names point.
func TestRealTestsIgnoreTheOperatorEnvironmentTheyAreRunFrom(t *testing.T) {
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	// Every name the identity golden lists for the tool but HOME, and the operator's own (the sandbox
	// switch, the operator config, its directory and the cache), points at a directory of this test,
	// as a live daemon's would.
	live := t.TempDir()
	names := []string{"VIBESYS_CLI_CONFIG", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"}
	for _, n := range golden(t) {
		if slices.Contains(n.Readers, "cli") && n.Name != "HOME" {
			names = append(names, n.Name)
		}
	}
	env := append(os.Environ(), production.EnvSandbox+"=1")
	var dirs []string
	for _, name := range names {
		dir := filepath.Join(live, name)
		if err := os.Mkdir(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		dirs = append(dirs, dir)
		env = append(env, name+"="+dir)
	}
	ctx, cancel := context.WithTimeout(context.Background(), hangBound)
	defer cancel()
	cmd := exec.CommandContext(ctx, self, "-test.run=^TestRealCheckThatFailsFailsTheJobAndSavesNothing$", "-test.count=1", "-test.v")
	cmd.Env = env
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	if err := cmd.Run(); err != nil {
		t.Fatalf("the tests, run with an operator's environment: %v\n%s", err, out.String())
	}
	if !strings.Contains(out.String(), "--- PASS: TestRealCheckThatFailsFailsTheJobAndSavesNothing") {
		t.Fatalf("the tests ran nothing:\n%s", out.String())
	}
	for _, d := range dirs {
		entries, _ := os.ReadDir(d)
		if len(entries) != 0 {
			t.Errorf("the tests wrote %v under the operator's %s", entries, d)
		}
	}
}
