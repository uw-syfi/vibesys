package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
)

var full = core.Commit("0123456789abcdef0123456789abcdef01234567")

// cli is the command line tool run in process, against a daemon that runs in this process over
// fakes: a real socket, a real client, no real environment.
type cli struct {
	t       *testing.T
	world   *daemontest.World
	starter *daemontest.Starter
	timers  *clock.ManualTimers
	git     *gitrepo.Fake
	env     map[string]string
}

func newCLI(t *testing.T) *cli {
	t.Helper()
	c := &cli{t: t, world: daemontest.NewWorld(t), timers: clock.NewManualTimers(), git: gitrepo.NewFake(), env: map[string]string{}}
	c.git.AddRepo("/repo", map[string]core.Commit{"HEAD": full, "main": full})
	dir := daemontest.RuntimeDir(t)
	c.starter = &daemontest.Starter{Dir: dir, Version: version, Runtime: c.world.Runtime}
	t.Cleanup(func() { c.starter.Close(t) })
	return c
}

// result is what one invocation did.
type result struct {
	code           int
	stdout, stderr string
}

func (c *cli) run(args ...string) result {
	c.t.Helper()
	var stdout, stderr bytes.Buffer
	d := deps{
		stdout: &stdout, stderr: &stderr,
		getenv: func(k string) string { return c.env[k] },
		cwd:    "/repo",
		git:    c.git,
		options: func() (client.Options, error) {
			return client.Options{Dir: c.starter.Dir, Version: version, Starter: c.starter, Timers: c.timers}, nil
		},
	}
	code := run(args, d)
	return result{code, stdout.String(), stderr.String()}
}

// withTask is the arguments of a command that names its task, placed after the command's name (and
// after the kind of job, for submit).
func withTask(args ...string) []string {
	at := 1
	if args[0] == "submit" {
		at = 2
	}
	return append(append(append([]string{}, args[:at]...), "--task", "kv"), args[at:]...)
}

// submit queues a command job and returns its id.
func (c *cli) submit(argv ...string) string {
	c.t.Helper()
	r := c.run(append(withTask("submit", "command", "HEAD", "--"), argv...)...)
	if r.code != exitOK {
		c.t.Fatalf("submit: exit %d: %s", r.code, r.stderr)
	}
	return strings.Fields(r.stdout)[0]
}

func TestVersionNamesTheBinaryAndExitsZero(t *testing.T) {
	c := newCLI(t)
	r := c.run("--version")
	if r.code != exitOK || r.stdout != "vibesys-cli "+version+"\n" || r.stderr != "" {
		t.Fatalf("result = %+v", r)
	}
}

// An invocation that names no command is invalid input: nothing is written to stdout, where a
// caller reads results, and the exit code is the one the contract reserves for it. No daemon is
// started for it.
func TestAnyUnknownCommandIsInvalidInput(t *testing.T) {
	c := newCLI(t)
	property := func(args []string) bool {
		if len(args) > 0 {
			if _, known := commands[args[0]]; known {
				return true
			}
			switch args[0] {
			case "--version", "--help", "-h", "help":
				return true
			}
		}
		r := c.run(args...)
		return r.code == exitInvalidInput && r.stdout == "" && r.stderr != ""
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
	if c.starter.Calls() != 0 {
		t.Fatal("an invalid invocation started a daemon")
	}
}

// Every mistake in the arguments is exit 2 with the offending flag or argument named, and the
// daemon is never reached for it.
func TestInvalidArgumentsAreRefusedBeforeTheDaemon(t *testing.T) {
	c := newCLI(t)
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"no kind", []string{"submit"}, "kind of job"},
		{"unknown kind", []string{"submit", "bogus"}, "bogus"},
		{"no task", []string{"submit", "command", "HEAD", "--", "true"}, "--task"},
		{"no command", withTask("submit", "command", "HEAD"), "after --"},
		{"empty command", withTask("submit", "command", "HEAD", "--"), "after --"},
		{"no commit", withTask("submit", "command", "--", "true"), "one commit"},
		{"bad env", withTask("submit", "command", "HEAD", "--env", "vm", "--", "true"), "--env"},
		{"probe without service", withTask("submit", "command", "HEAD", "--probe", "ok", "--", "true"), "--probe"},
		{"negative timeout", withTask("submit", "command", "HEAD", "--timeout=-1s", "--", "true"), "--timeout"},
		{"bad flag", withTask("submit", "command", "HEAD", "--nope", "--", "true"), "nope"},
		{"wait no jobs", withTask("wait"), "at least one job"},
		{"wait negative timeout", withTask("wait", "j1", "--timeout=-1s"), "--timeout"},
		{"cancel two jobs", withTask("cancel", "j1", "j2"), "one job"},
		{"note one argument", withTask("note", "j1"), "text"},
		{"jobs bad state", withTask("jobs", "--state", "stuck"), "stuck"},
		{"jobs bad kind", withTask("jobs", "--kind", "nap"), "--kind"},
		{"jobs extra argument", withTask("jobs", "j1"), "j1"},
		{"logs no job", withTask("logs"), "one job"},
		{"daemon nothing", []string{"daemon"}, "status, stop or restart"},
		{"daemon bogus", []string{"daemon", "bogus"}, "bogus"},
		{"unknown commit", withTask("submit", "command", "no-such-rev", "--", "true"), "no-such-rev"},
	}
	for _, tc := range cases {
		r := c.run(tc.args...)
		wantCode := exitInvalidInput
		if tc.name == "unknown commit" {
			wantCode = exitFailed
		}
		if r.code != wantCode || r.stdout != "" || !strings.Contains(r.stderr, tc.want) {
			t.Errorf("%s: exit %d, stdout %q, stderr %q; want exit %d, no stdout, and %q in stderr", tc.name, r.code, r.stdout, r.stderr, wantCode, tc.want)
		}
	}
	if c.starter.Calls() != 0 {
		t.Fatal("an invalid invocation started a daemon")
	}
}

func TestSubmitWaitAndLogs(t *testing.T) {
	c := newCLI(t)
	id := c.submit("print", "hello", ";", "exit", "0")
	r := c.run(withTask("wait", id)...)
	if r.code != exitOK || !strings.Contains(r.stdout, id+"\tsucceeded (exit 0)\t0123456789ab\tprint hello ; exit 0") {
		t.Fatalf("wait = %+v", r)
	}
	if r := c.run(withTask("logs", id)...); r.code != exitOK || r.stdout != "hello\n" {
		t.Fatalf("logs = %+v", r)
	}
}

// Flags and jobs may come in any order after the command.
func TestWaitTakesFlagsAfterTheJobs(t *testing.T) {
	c := newCLI(t)
	a := c.submit("exit", "0")
	b := c.submit("exit", "0")
	r := c.run("wait", a, b, "--task", "kv", "--any")
	if r.code != exitOK {
		t.Fatalf("wait = %+v", r)
	}
}

// wait's exit code is 1 when a job failed, whichever of the jobs it was.
func TestWaitExitsOneWhenAJobFailed(t *testing.T) {
	c := newCLI(t)
	ok := c.submit("exit", "0")
	bad := c.submit("exit", "3")
	r := c.run(withTask("wait", ok, bad)...)
	if r.code != exitFailed || !strings.Contains(r.stdout, bad+"\tfailed (exit 3)") {
		t.Fatalf("wait = %+v", r)
	}
}

// A timeout is exit 3 and says the job was not cancelled; the job then succeeds.
func TestWaitTimeoutIsExitThreeAndTheJobGoesOn(t *testing.T) {
	c := newCLI(t)
	id := c.submit("hold", "g", ";", "exit", "0")

	done := make(chan result, 1)
	go func() { done <- c.run(withTask("wait", id, "--timeout", "30m")...) }()
	if d := <-c.timers.Armed(); d != 30*time.Minute {
		t.Fatalf("the timeout armed was %v, want 30m", d)
	}
	c.timers.Fire()
	r := <-done
	if r.code != exitTimeout || !strings.Contains(r.stderr, "never cancels") {
		t.Fatalf("wait = %+v, want exit 3 saying a timeout never cancels a job", r)
	}

	c.world.Gates.Release("g")
	if r := c.run(withTask("wait", id)...); r.code != exitOK {
		t.Fatalf("the job after a timed-out wait: %+v", r)
	}
}

func TestSubmitWaitBlocksAndExitsLikeWait(t *testing.T) {
	c := newCLI(t)
	r := c.run(withTask("submit", "command", "HEAD", "--wait", "--", "print", "x", ";", "exit", "0")...)
	if r.code != exitOK || strings.Count(r.stdout, "\n") != 2 || !strings.Contains(r.stdout, "succeeded") {
		t.Fatalf("submit --wait = %+v, want the queued line and the final line", r)
	}
	r = c.run(withTask("submit", "command", "HEAD", "--wait", "--", "exit", "4")...)
	if r.code != exitFailed {
		t.Fatalf("submit --wait of a failing command: exit %d, want 1", r.code)
	}
}

func TestCancelNamesTheReasonToWaiters(t *testing.T) {
	c := newCLI(t)
	id := c.submit("hold", "g", ";", "exit", "0")
	r := c.run(withTask("cancel", id, "--reason", "superseded")...)
	if r.code != exitOK || !strings.Contains(r.stdout, "cancelled (superseded)") {
		t.Fatalf("cancel = %+v", r)
	}
	// A waiter sees the same end, and it is not a success.
	if r := c.run(withTask("wait", id)...); r.code != exitFailed || !strings.Contains(r.stdout, "cancelled (superseded)") {
		t.Fatalf("wait after cancel = %+v", r)
	}
}

func TestNoteAndJobsFilters(t *testing.T) {
	c := newCLI(t)
	a := c.submit("exit", "0")
	b := c.submit("exit", "1")
	c.run(withTask("wait", a, b)...)
	if r := c.run(withTask("note", a, "tried the cheap path")...); r.code != exitOK || !strings.Contains(r.stdout, "note: tried the cheap path") {
		t.Fatalf("note = %+v", r)
	}
	list := func(args ...string) string {
		r := c.run(withTask(append([]string{"jobs"}, args...)...)...)
		if r.code != exitOK {
			t.Fatalf("jobs %v = %+v", args, r)
		}
		return r.stdout
	}
	if out := list(); !strings.Contains(out, a) || !strings.Contains(out, b) {
		t.Fatalf("jobs = %q, want both", out)
	}
	if out := list("--state", "failed"); strings.Contains(out, a) || !strings.Contains(out, b) {
		t.Fatalf("jobs --state failed = %q", out)
	}
	if out := list("--state", "failed,succeeded"); !strings.Contains(out, a) || !strings.Contains(out, b) {
		t.Fatalf("jobs --state failed,succeeded = %q", out)
	}
	if out := list("--note-contains", "cheap"); !strings.Contains(out, a) || strings.Contains(out, b) {
		t.Fatalf("jobs --note-contains = %q", out)
	}
	if out := list("--commit", "main", "--kind", "command"); !strings.Contains(out, a) || !strings.Contains(out, b) {
		t.Fatalf("jobs --commit main = %q", out)
	}
}

// --json is one JSON message per line, in the contract's field names, on every command.
func TestJSONOutput(t *testing.T) {
	c := newCLI(t)
	r := c.run(withTask("submit", "command", "HEAD", "--json", "--", "print", "j", ";", "exit", "0")...)
	var submitted struct {
		Job struct {
			ID string `json:"id"`
		} `json:"job"`
		Disposition string `json:"disposition"`
	}
	if r.code != exitOK || json.Unmarshal([]byte(r.stdout), &submitted) != nil || submitted.Job.ID == "" {
		t.Fatalf("submit --json = %+v", r)
	}
	if submitted.Disposition != "SUBMIT_DISPOSITION_CREATED" {
		t.Fatalf("disposition = %q", submitted.Disposition)
	}
	id := submitted.Job.ID

	for _, args := range [][]string{
		{"wait", id}, {"jobs"}, {"cancel", id}, {"note", id, "text"}, {"logs", id},
	} {
		r := c.run(append(withTask(args...), "--json")...)
		if r.code != exitOK && args[0] != "wait" {
			t.Errorf("%v --json: %+v", args, r)
			continue
		}
		lines := strings.Split(strings.TrimSuffix(r.stdout, "\n"), "\n")
		if len(lines) == 0 || lines[0] == "" {
			t.Errorf("%v --json wrote nothing", args)
		}
		for _, line := range lines {
			var v map[string]any
			if err := json.Unmarshal([]byte(line), &v); err != nil {
				t.Errorf("%v --json: %q is not JSON: %v", args, line, err)
			}
		}
	}
}

// With --json an error is a JSON Failure on stderr, and stdout stays empty.
func TestJSONErrors(t *testing.T) {
	c := newCLI(t)
	r := c.run(withTask("wait", "j404", "--json")...)
	var f struct{ Kind, Message string }
	if r.code != exitFailed || r.stdout != "" || json.Unmarshal([]byte(r.stderr), &f) != nil || f.Kind != "FAILURE_KIND_NOT_FOUND" {
		t.Fatalf("wait of an unknown job --json = %+v", r)
	}
}

func TestSubmitOfLaterKindsIsUnavailable(t *testing.T) {
	c := newCLI(t)
	for _, kind := range []string{"check", "measure"} {
		r := c.run(withTask("submit", kind, "HEAD")...)
		if r.code != exitFailed || !strings.Contains(r.stderr, "not available in this build") {
			t.Errorf("submit %s = %+v", kind, r)
		}
	}
	if c.starter.Calls() != 0 {
		t.Fatal("a refused submission started a daemon")
	}
}

func TestEnvironmentVariablesFillTheCallContext(t *testing.T) {
	c := newCLI(t)
	c.env["VIBESYS_TASK"] = "kv"
	c.env["VIBESYS_RUN"] = "run-7"
	c.env["VIBESYS_AGENT"] = "agent-3"
	r := c.run("submit", "command", "HEAD", "--note", "n", "--", "exit", "0")
	if r.code != exitOK {
		t.Fatalf("submit = %+v", r)
	}
}

func TestDaemonStatusStopRestart(t *testing.T) {
	c := newCLI(t)
	if r := c.run("daemon", "status"); r.code != exitFailed || r.stdout != "not running\n" {
		t.Fatalf("status with no daemon = %+v", r)
	}
	if r := c.run("daemon", "stop"); r.code != exitOK || r.stdout != "not running\n" {
		t.Fatalf("stop with no daemon = %+v", r)
	}
	if c.starter.Calls() != 0 {
		t.Fatal("status or stop started a daemon")
	}
	c.submit("exit", "0")
	if r := c.run("daemon", "status"); r.code != exitOK || !strings.HasPrefix(r.stdout, "running\t") {
		t.Fatalf("status = %+v", r)
	}
	if r := c.run("daemon", "restart"); r.code != exitOK || !strings.HasPrefix(r.stdout, "restarted\t") {
		t.Fatalf("restart = %+v", r)
	}
	if c.starter.Calls() != 2 {
		t.Fatalf("starts = %d, want 2", c.starter.Calls())
	}
	if r := c.run("daemon", "stop"); r.code != exitOK || r.stdout != "stopped\n" {
		t.Fatalf("stop = %+v", r)
	}
	if r := c.run("daemon", "status"); r.code != exitFailed {
		t.Fatalf("status after stop = %+v", r)
	}
	if r := c.run("daemon", "install"); r.code != exitFailed || !strings.Contains(r.stderr, "not available") {
		t.Fatalf("install = %+v", r)
	}
}

// Commands that run at the same moment against no daemon start one between them.
func TestConcurrentCommandsStartOneDaemon(t *testing.T) {
	c := newCLI(t)
	var wg sync.WaitGroup
	for range 6 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if r := c.run(withTask("jobs")...); r.code != exitOK {
				t.Errorf("jobs = %+v", r)
			}
		}()
	}
	wg.Wait()
	if c.starter.Calls() != 1 {
		t.Fatalf("starts = %d, want 1", c.starter.Calls())
	}
}
