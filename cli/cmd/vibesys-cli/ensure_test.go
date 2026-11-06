package main

import (
	"encoding/json"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
	"strings"
	"sync"
	"testing"
)

// ensure is the one way to start the daemon for a program that reads the exit code only.
func TestDaemonEnsureStartsADaemonOnceAndLaterEnsuresUseIt(t *testing.T) {
	c := newCLI(t)
	r := c.run("daemon", "ensure")
	if r.code != exitOK || !strings.HasPrefix(r.stdout, "ready\tpid ") {
		t.Fatalf("ensure with no daemon = %+v", r)
	}
	if c.starter.Calls() != 1 {
		t.Fatalf("starts = %d, want 1", c.starter.Calls())
	}
	if r := c.run("daemon", "ensure"); r.code != exitOK {
		t.Fatalf("second ensure = %+v", r)
	}
	if r := c.run("daemon", "status"); r.code != exitOK {
		t.Fatalf("status after ensure = %+v", r)
	}
	if c.starter.Calls() != 1 {
		t.Fatalf("starts = %d after a daemon answered, want 1", c.starter.Calls())
	}
}

// Whatever the reason no daemon answers, the exit code is the same, and the message says why.
func TestDaemonEnsureEndsWithNoAnswerWhenNoDaemonCanBeStarted(t *testing.T) {
	c := newCLI(t)
	c.cannotStart = true
	r := c.run("daemon", "ensure")
	if r.code != exitNoAnswer || r.stdout != "" || r.stderr == "" {
		t.Fatalf("ensure = %+v, want exit %d and a message on stderr", r, exitNoAnswer)
	}
	if c.starter.Calls() != 0 {
		t.Fatal("ensure started a daemon it was not given the means to start")
	}
	r = c.run("daemon", "ensure", "--json")
	if r.code != exitNoAnswer || !json.Valid([]byte(r.stderr)) && !json.Valid([]byte(r.stdout)) {
		t.Fatalf("ensure --json = %+v, want exit %d and a JSON error", r, exitNoAnswer)
	}
}

// A command that is itself malformed is the one case that ends in 2.
func TestDaemonEnsureRejectsAMalformedCommand(t *testing.T) {
	c := newCLI(t)
	for _, args := range [][]string{{"daemon", "ensure", "--nope"}} {
		if r := c.run(args...); r.code != exitInvalidInput {
			t.Fatalf("%v = %+v, want exit %d", args, r, exitInvalidInput)
		}
	}
	if c.starter.Calls() != 0 {
		t.Fatal("a malformed ensure started a daemon")
	}
}

func TestDaemonEnsureJSONIsTheStatus(t *testing.T) {
	c := newCLI(t)
	r := c.run("daemon", "ensure", "--json")
	if r.code != exitOK {
		t.Fatalf("ensure --json = %+v", r)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(r.stdout), &got); err != nil {
		t.Fatalf("stdout %q is not JSON: %v", r.stdout, err)
	}
}

// Ensures that run at the same moment as commands that start a daemon themselves share the one
// start: the start.lock the client holds is the only protocol.
func TestEnsureRacingCommandsStartsOneDaemon(t *testing.T) {
	c := newCLI(t)
	var wg sync.WaitGroup
	codes := make(chan int, 8)
	for i := range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if i%2 == 0 {
				codes <- c.run("daemon", "ensure").code
			} else {
				codes <- c.run(withTask("jobs")...).code
			}
		}()
	}
	wg.Wait()
	close(codes)
	for code := range codes {
		if code != exitOK {
			t.Fatalf("a racing command ended with %d", code)
		}
	}
	if c.starter.Calls() != 1 {
		t.Fatalf("starts = %d, want 1", c.starter.Calls())
	}
}

// The live finding: a bundled release command line met the daemon of an older local build that
// was already running for the user, used it, and every manifest was refused by the old daemon with
// a message that named neither it nor the file. The command line now replaces an idle daemon of
// another build with its own.
func TestDaemonEnsureReplacesAnIdleDaemonOfAnotherBuild(t *testing.T) {
	c := newCLI(t)
	c.starter.Version = "0.1.0-local.9342cd403"
	c.build = "0.1.0-local.9342cd403"
	if r := c.run("daemon", "ensure"); r.code != exitOK {
		t.Fatalf("first ensure = %+v", r)
	}
	c.build = "0.0.1-live"
	c.starter.Version = "0.0.1-live"
	r := c.run("daemon", "ensure")
	if r.code != exitOK || !strings.Contains(r.stdout, "version 0.0.1-live") {
		t.Fatalf("ensure from another build = %+v, want a daemon of build 0.0.1-live", r)
	}
	if c.starter.Calls() != 2 {
		t.Fatalf("starts = %d, want the old daemon replaced once", c.starter.Calls())
	}
}

// With a job on the old daemon the command line leaves it alone and says why: a failure of the
// named kind, exit 4, both builds, the daemon and what to do. status still shows that daemon.
func TestDaemonEnsureRefusesADaemonOfAnotherBuildThatHasAJob(t *testing.T) {
	c := newCLI(t)
	c.starter.Version = "0.1.0-local.9342cd403"
	c.build = "0.1.0-local.9342cd403"
	if r := c.run("daemon", "ensure"); r.code != exitOK {
		t.Fatalf("first ensure = %+v", r)
	}
	c.submit("print", "x", ";", "hold", "g", ";", "exit", "0")
	c.build = "0.0.1-live"
	c.starter.Version = "0.0.1-live"
	r := c.run("daemon", "ensure")
	if r.code != exitNoAnswer || r.stdout != "" {
		t.Fatalf("ensure = %+v, want exit %d with the reason on stderr", r, exitNoAnswer)
	}
	for _, want := range []string{"0.0.1-live", "0.1.0-local.9342cd403", "daemon restart", "pid "} {
		if !strings.Contains(r.stderr, want) {
			t.Errorf("the refusal %q does not say %q", r.stderr, want)
		}
	}
	j := c.run("daemon", "ensure", "--json")
	var failure struct{ Kind string }
	if err := json.Unmarshal([]byte(j.stderr), &failure); err != nil || failure.Kind != "FAILURE_KIND_DAEMON_OTHER_BUILD" {
		t.Fatalf("ensure --json = %+v (%v), want the kind FAILURE_KIND_DAEMON_OTHER_BUILD", j, err)
	}
	if c.starter.Calls() != 1 {
		t.Fatalf("starts = %d: the daemon with a job was replaced", c.starter.Calls())
	}
	if s := c.run("daemon", "status"); s.code != exitOK || !strings.Contains(s.stdout, "version 0.1.0-local.9342cd403") {
		t.Fatalf("status = %+v, want the old daemon still answering", s)
	}
	c.world.Gates.Release("g")
}

// A task the daemon cannot read is refused with the daemon's build and the manifest's file, so
// that an agent that reads it reports the daemon and the file and not a bare TOML error.
func TestATaskTheDaemonCannotReadNamesTheDaemonAndTheFile(t *testing.T) {
	c := newCLI(t)
	files := tasktest.Files("kv")
	files[".vibesys/tasks/kv/vibesys.input.toml"] = task.File{Data: []byte("version = 1\n")}
	c.world.Tasks.Put("/repo", full, files)
	r := c.run(withTask("submit", "check", "HEAD", "--wait")...)
	if r.code != exitInvalidInput {
		t.Fatalf("submit check = %+v, want exit %d", r, exitInvalidInput)
	}
	for _, want := range []string{"daemon of build " + version, "vibesys.input.toml", ".vibesys/tasks/kv"} {
		if !strings.Contains(r.stderr, want) {
			t.Errorf("the refusal %q does not say %q", r.stderr, want)
		}
	}
}
