package main

import (
	"encoding/json"
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
