package main

import (
	"bytes"
	"encoding/json"
	"os"
	"slices"
	"sync"
	"testing"
)

// goldenPath is the one list of environment names the orchestrator gives an agent. The orchestrator's
// sidecar tests read the same file and check the agent gets exactly those names.
const goldenPath = "../../../proto/vibesys/cli/v1/conformance/identity_env.golden.json"

type goldenName struct {
	Name    string   `json:"name"`
	Readers []string `json:"readers"`
}

func golden(t *testing.T) []goldenName {
	t.Helper()
	data, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatal(err)
	}
	var g struct {
		Names []goldenName `json:"names"`
	}
	if err := json.Unmarshal(data, &g); err != nil {
		t.Fatalf("%s: %v", goldenPath, err)
	}
	return g.Names
}

// recorder is a getenv that remembers every name asked of it.
type recorder struct {
	mu    sync.Mutex
	asked map[string]bool
	env   map[string]string
}

func (r *recorder) getenv(name string) string {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.asked[name] = true
	return r.env[name]
}

func (r *recorder) names() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, 0, len(r.asked))
	for name := range r.asked {
		out = append(out, name)
	}
	slices.Sort(out)
	return out
}

// Every command line, and the binary's wiring to its daemon and store, reads its environment through
// the one getenv it is given, and the names it asks for are exactly the ones the golden lists with
// the vibesys-cli as a reader. A name the CLI starts to read that the orchestrator does not set, or
// one the orchestrator sets for the CLI that the CLI no longer reads, fails here.
func TestTheCLIReadsExactlyTheNamesTheGoldenListsForIt(t *testing.T) {
	rec := &recorder{asked: map[string]bool{}, env: map[string]string{
		"XDG_RUNTIME_DIR": "/run/user/1", "XDG_STATE_HOME": "/state", "HOME": "/home/u",
	}}
	var stdout, stderr bytes.Buffer
	d := depsFrom(&stdout, &stderr, rec.getenv)
	// -h makes each command build its flags, which is where the context flags read their defaults,
	// and then stop, so none of them reaches a daemon.
	for _, args := range [][]string{
		{"submit", "check"}, {"submit", "measure"}, {"submit", "command"}, {"wait"}, {"cancel"}, {"note"}, {"jobs"}, {"logs"},
		{"show"}, {"compare"}, {"promote"}, {"result"}, {"plan", "create"}, {"plan", "list"}, {"plan", "view"},
		{"plan", "edit"}, {"plan", "comment"}, {"plan", "close"}, {"daemon", "status"}, {"daemon", "stop"},
		{"daemon", "restart"}, {"daemon", "forget"},
	} {
		if code := run(append(slices.Clone(args), "-h"), d); code != exitOK {
			t.Fatalf("%v -h exited %d: %s", args, code, stderr.String())
		}
	}
	// Reaching the daemon starts from where the runtime directory is, and the daemon process from
	// where the task index is. Neither is opened: that touches the file system.
	_ = d.process.RuntimePath()
	_ = d.process.IndexDir()

	var want []string
	for _, n := range golden(t) {
		if slices.Contains(n.Readers, "cli") {
			want = append(want, n.Name)
		}
	}
	slices.Sort(want)
	if got := rec.names(); !slices.Equal(got, want) {
		t.Fatalf("the CLI read %v, the golden lists %v", got, want)
	}
}
