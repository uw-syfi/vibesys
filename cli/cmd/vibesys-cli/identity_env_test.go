package main

import (
	"bytes"
	"regexp"
	"slices"
	"strings"
	"sync"
	"testing"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// contextMessages are the messages the binary reads its context from.
var contextMessages = []proto.Message{&cliv1.AgentContext{}, &cliv1.OperatorContext{}}

func declaredContextNames(t *testing.T) []string {
	t.Helper()
	var names []string
	for _, m := range contextMessages {
		some, err := envcodec.Names(m)
		if err != nil {
			t.Fatal(err)
		}
		names = append(names, some...)
	}
	slices.Sort(names)
	return names
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
// the one getenv it is given, and the VIBESYS_ names it asks for are exactly the fields of the
// context messages. A name the binary starts to read that no message declares fails here, as does a
// field it no longer reads. The places of its daemon and store (HOME and the XDG names) are the job
// tool's endpoint, a message of its own later, and are not held here.
func TestTheCLIReadsExactlyTheNamesTheContextMessagesDeclare(t *testing.T) {
	want := declaredContextNames(t)
	rec := &recorder{asked: map[string]bool{}, env: map[string]string{}}
	for _, name := range want {
		rec.env[name] = "/value/" + strings.ToLower(name)
	}
	rec.env["VIBESYS_HARNESS"] = "claude-code"
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

	var got []string
	for _, name := range rec.names() {
		if strings.HasPrefix(name, "VIBESYS_") {
			got = append(got, name)
		}
	}
	if !slices.Equal(got, want) {
		t.Fatalf("the CLI read %v, the messages declare %v", got, want)
	}
}

// The usage text names the variables the flags default to, and those are the declared ones: a field
// added to a context message that the help does not name, or a name in the help that no field has,
// fails here.
func TestTheUsageNamesTheDeclaredVariables(t *testing.T) {
	named := regexp.MustCompile(`VIBESYS_[A-Z_]*[A-Z]`).FindAllString(usage, -1)
	slices.Sort(named)
	if want := declaredContextNames(t); !slices.Equal(slices.Compact(named), want) {
		t.Fatalf("the usage names %v, the context messages declare %v", slices.Compact(named), want)
	}
}

// A bad value is refused where a command builds its context, naming the variable and not the value.
func TestABadContextVariableIsRefusedNamingIt(t *testing.T) {
	var stdout, stderr bytes.Buffer
	env := map[string]string{"VIBESYS_TASK": "kv", "VIBESYS_HARNESS": "Not A Harness"}
	d := depsFrom(&stdout, &stderr, func(name string) string { return env[name] })
	if code := run([]string{"jobs"}, d); code != exitInvalidInput {
		t.Fatalf("exit %d, want a usage error: %s%s", code, stdout.String(), stderr.String())
	}
	if out := stdout.String() + stderr.String(); !strings.Contains(out, "VIBESYS_HARNESS") || strings.Contains(out, "Not A Harness") {
		t.Fatalf("the refusal must name the variable and not repeat its value: %s", out)
	}
}
