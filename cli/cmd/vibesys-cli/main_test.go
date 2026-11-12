package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"testing"
	"testing/quick"
	"time"
	"unicode/utf8"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/production"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
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
	// stdin and files are what a command reads when given "-" or a path.
	stdin string
	files map[string]string
	// cannotStart is a machine on which no daemon can be started.
	cannotStart bool
	// cwd is the directory the command runs in, /repo unless a test moves it.
	cwd string
	// build is the build identity the command line reports, the test binary's version unless a
	// test sets another.
	build string
}

func (c *cli) buildIdentity() string {
	if c.build != "" {
		return c.build
	}
	return version
}

func newCLI(t *testing.T) *cli {
	t.Helper()
	w := daemontest.NewWorld(t)
	// The command line and the daemon see one set of repositories, as they do on one machine.
	c := &cli{t: t, world: w, timers: clock.NewManualTimers(), git: w.Git, env: map[string]string{}, files: map[string]string{}, cwd: "/repo"}
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
		cwd:    c.cwd,
		git:    c.git,
		stdin:  strings.NewReader(c.stdin),
		readFile: func(path string) ([]byte, error) {
			if text, ok := c.files[path]; ok {
				return []byte(text), nil
			}
			return nil, fmt.Errorf("open %s: no such file or directory", path)
		},
		options: func() (client.Options, error) {
			var starter client.Starter = c.starter
			if c.cannotStart {
				starter = nil
			}
			return client.Options{Rendezvous: client.LocalRendezvous{Dir: c.starter.Dir}, Version: c.buildIdentity(), Starter: starter, Timers: c.timers,
				// The command line's time holds still, so a wait asks the daemon for its whole timeout.
				Clock: clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), 0)}, nil
		},
	}
	code := run(args, d)
	return result{code, stdout.String(), stderr.String()}
}

// withTask is the arguments of a command that names its task, placed after the command's name (and
// after the kind of job, for submit).
func withTask(args ...string) []string {
	at := 1
	if args[0] == "submit" || args[0] == "frontier" {
		at = 2
	}
	return append(append(append([]string{}, args[:at]...), "--task", "kv"), args[at:]...)
}

// submit queues a command job and returns its id.
func (c *cli) submit(argv ...string) string {
	c.t.Helper()
	r := c.run(append(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--"), argv...)...)
	if r.code != exitOK {
		c.t.Fatalf("submit: exit %d: %s", r.code, r.stderr)
	}
	return strings.Fields(r.stdout)[0]
}

func TestVersionNamesTheBinaryAndExitsZero(t *testing.T) {
	c := newCLI(t)
	r := c.run("--version")
	if r.code != exitOK || r.stdout != "vibesys-cli "+version+" (task schemas 0.1, 0.2)\n" || r.stderr != "" {
		t.Fatalf("result = %+v", r)
	}
}

// `--version --json` prints the versions the build speaks as one typed message, for a program that
// asks before it uses the tool, and starts no daemon.
func TestVersionJSONSaysWhatTheBuildSpeaks(t *testing.T) {
	c := newCLI(t)
	r := c.run("--version", "--json")
	var got cliv1.BuildVersions
	if r.code != exitOK || r.stderr != "" || protojson.Unmarshal([]byte(r.stdout), &got) != nil {
		t.Fatalf("result = %+v", r)
	}
	want := wire.BuildVersions(version, production.CurrentVersions())
	if !proto.Equal(&got, want) || got.GetProtocolVersion() < core.TaskSchema02Since {
		t.Errorf("printed %v, want %v at protocol %d or above", &got, want, core.TaskSchema02Since)
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
		{"no task", []string{"submit", "command", "--image", "harness", "HEAD", "--", "true"}, "--task"},
		{"no command", withTask("submit", "command", "--image", "harness", "HEAD"), "after --"},
		{"empty command", withTask("submit", "command", "--image", "harness", "HEAD", "--"), "after --"},
		{"no commit", withTask("submit", "command", "--image", "harness", "--", "true"), "one commit"},
		{"bad env", withTask("submit", "command", "--image", "harness", "HEAD", "--env", "vm", "--", "true"), "--env"},
		{"an unended wrap", withTask("submit", "command", "--image", "harness", "HEAD", "--wrap", "nsys 'profile", "--", "true"), "--wrap"},
		{"profile, retired", withTask("submit", "profile", "HEAD"), "--image harness --wrap"},
		{"command with no image", withTask("submit", "command", "HEAD", "--", "true"), "--image is required: harness"},
		{"command with an unknown image", withTask("submit", "command", "HEAD", "--image", "task", "--", "true"), "neither harness"},
		{"candidate on the host", withTask("submit", "command", "HEAD", "--image", "candidate", "--env", "host", "--", "true"), "--env host"},
		{"check with a command", withTask("submit", "check", "HEAD", "--", "true"), "own commands"},
		{"check with samples", withTask("submit", "check", "HEAD", "--samples", "3"), "--samples"},
		{"check with recheck", withTask("submit", "check", "HEAD", "--recheck"), "--recheck"},
		{"measure with no commit", withTask("submit", "measure"), "one commit"},
		{"measure with too many samples", withTask("submit", "measure", "HEAD", "--samples", "99999999999"), "--samples"},
		{"measure with a bad env", withTask("submit", "measure", "HEAD", "--env", "vm"), "--env"},
		{"measure with both plan flags names --plan", withTask("submit", "measure", "HEAD", "--plan", "1", "--no-plan"), "--plan"},
		{"measure with both plan flags names --no-plan", withTask("submit", "measure", "HEAD", "--plan", "1", "--no-plan"), "--no-plan"},
		{"measure with plan 0", withTask("submit", "measure", "HEAD", "--plan", "0"), "--plan"},
		{"measure with a plan number no plan has", withTask("submit", "measure", "HEAD", "--plan", "99999999999"), "--plan"},
		{"check with no plan", withTask("submit", "check", "HEAD", "--no-plan"), "--no-plan"},
		{"command with no plan", withTask("submit", "command", "--image", "harness", "HEAD", "--no-plan", "--", "true"), "no-plan"},
		{"command with plan 0", withTask("submit", "command", "--image", "harness", "HEAD", "--plan", "0", "--", "true"), "--plan"},
		{"command on the host with a placement", withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--placement", "local", "--", "true"), "--placement"},
		{"check on the host with a placement", withTask("submit", "check", "HEAD", "--placement=local", "--env", "host"), "--placement"},
		{"probe without service", withTask("submit", "command", "--image", "harness", "HEAD", "--probe", "ok", "--", "true"), "--probe"},
		{"negative timeout", withTask("submit", "command", "--image", "harness", "HEAD", "--timeout=-1s", "--", "true"), "--timeout"},
		{"bad flag", withTask("submit", "command", "--image", "harness", "HEAD", "--nope", "--", "true"), "nope"},
		{"wait no jobs", withTask("wait"), "at least one job"},
		{"wait negative timeout", withTask("wait", "j1", "--timeout=-1s"), "--timeout"},
		{"cancel two jobs", withTask("cancel", "j1", "j2"), "one job"},
		{"note one argument", withTask("note", "j1"), "text"},
		{"jobs bad state", withTask("jobs", "--state", "stuck"), "stuck"},
		{"jobs bad kind", withTask("jobs", "--kind", "nap"), "--kind"},
		{"jobs extra argument", withTask("jobs", "j1"), "j1"},
		{"logs no job", withTask("logs"), "one job"},
		{"daemon nothing", []string{"daemon"}, "status, ensure, stop, restart or forget"},
		{"daemon bogus", []string{"daemon", "bogus"}, "bogus"},
		{"unknown commit", withTask("submit", "command", "--image", "harness", "no-such-rev", "--", "true"), "no-such-rev"},
	}
	for _, tc := range cases {
		r := c.run(tc.args...)
		wantCode := exitInvalidInput // an unknown commit is NOT_FOUND, which is invalid input too
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
	if r.code != exitOK || !strings.Contains(r.stdout, id+"\tsucceeded (exit 0)\t0123456789ab\t[harness] print hello ; exit 0") {
		t.Fatalf("wait = %+v", r)
	}
	if r := c.run(withTask("logs", id)...); r.code != exitOK || r.stdout != "hello\n" {
		t.Fatalf("logs = %+v", r)
	}
}

// --wrap puts its words in front of the command when there is no --service.
func TestSubmitCommandWrapsTheCommand(t *testing.T) {
	c := newCLI(t)
	r := c.run(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--wrap", "print wrapped ;", "--wait", "--", "print", "hello")...)
	if r.code != exitOK {
		t.Fatalf("submit = %+v", r)
	}
	id := strings.Fields(r.stdout)[0]
	if r := c.run(withTask("logs", id)...); r.code != exitOK || r.stdout != "wrapped\nhello\n" {
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

// waitTimeout is the --timeout the timeout tests give. No other wait of the daemon is this long,
// so the daemon's timers fire it alone, at once.
const waitTimeout = 45 * time.Minute

// A timeout is the daemon's answer, exit 3, and says the job was not cancelled; the job then
// succeeds.
func TestWaitTimeoutIsExitThreeAndTheJobGoesOn(t *testing.T) {
	c := newCLI(t)
	c.starter.Timers = clock.Exactly{D: waitTimeout, Others: clock.System{}}
	id := c.submit("hold", "g", ";", "exit", "0")
	r := c.run(withTask("wait", id, "--timeout", waitTimeout.String())...)
	if r.code != exitTimeout || !strings.Contains(r.stderr, "never cancels") || !strings.Contains(r.stdout, id) {
		t.Fatalf("wait = %+v, want exit 3 saying a timeout never cancels a job", r)
	}
	c.world.Gates.Release("g")
	if r := c.run(withTask("wait", id)...); r.code != exitOK {
		t.Fatalf("the job after a timed-out wait: %+v", r)
	}
}

func TestSubmitWaitBlocksAndExitsLikeWait(t *testing.T) {
	c := newCLI(t)
	r := c.run(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--wait", "--", "print", "x", ";", "exit", "0")...)
	if r.code != exitOK || strings.Count(r.stdout, "\n") != 2 || !strings.Contains(r.stdout, "succeeded") {
		t.Fatalf("submit --wait = %+v, want the queued line and the final line", r)
	}
	r = c.run(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--wait", "--", "exit", "4")...)
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
	r := c.run(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--json", "--", "print", "j", ";", "exit", "0")...)
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
	if r.code != exitInvalidInput || r.stdout != "" || json.Unmarshal([]byte(r.stderr), &f) != nil || f.Kind != "FAILURE_KIND_NOT_FOUND" {
		t.Fatalf("wait of an unknown job --json = %+v", r)
	}
}

// Whatever goes wrong, and however early, a command line with --json gets a Failure and no text.
func TestJSONErrorsFromBeforeTheCommandRuns(t *testing.T) {
	c := newCLI(t)
	for _, args := range [][]string{
		{"bogus", "--json"},
		{"--json"},
		{"wait", "--no-such-flag", "--json"},
		{"wait", "--json", "--no-such-flag"},
		{"wait", "--timeout", "soon", "--json"},
		{"jobs", "--state", "nonsense", "--json"},
		{"cancel", "--json", "--no-such-flag"},
		{"note", "--json", "--no-such-flag"},
		{"logs", "--json", "--no-such-flag"},
		{"submit", "command", "--image", "harness", "--json", "--no-such-flag", "--", "true"},
		{"daemon", "status", "--json", "--no-such-flag"},
		{"daemon", "stop", "--json", "--no-such-flag"},
		{"daemon", "restart", "--json", "--no-such-flag"},
		{"daemon", "bogus", "--json"},
	} {
		r := c.run(args...)
		var f struct{ Kind, Message string }
		if err := json.Unmarshal([]byte(r.stderr), &f); err != nil {
			t.Errorf("%v: stderr is not one Failure: %v: %q", args, err, r.stderr)
			continue
		}
		if r.code != exitInvalidInput || r.stdout != "" || f.Kind != "FAILURE_KIND_INVALID_INPUT" || f.Message == "" {
			t.Errorf("%v = %+v, failure %+v", args, r, f)
		}
	}
}

// With --json, wait prints the daemon's outcome as it sent it, on every way a wait ends.
func TestWaitUnderJSONPrintsTheDaemonsOutcome(t *testing.T) {
	c := newCLI(t)
	c.starter.Timers = clock.Exactly{D: waitTimeout, Others: clock.System{}}
	ok := c.submit("exit", "0")
	bad := c.submit("exit", "3")
	held := c.submit("hold", "g", ";", "exit", "0")
	cases := []struct {
		args     []string
		kind, by string
		code     int
	}{
		{[]string{"wait", ok}, "WAIT_OUTCOME_KIND_ALL_SUCCEEDED", "", exitOK},
		{[]string{"wait", ok, bad}, "WAIT_OUTCOME_KIND_SOME_FAILED", bad, exitFailed},
		{[]string{"wait", "--any", bad, ok}, "WAIT_OUTCOME_KIND_FIRST_FAILED", bad, exitFailed},
		{[]string{"wait", "--any", ok, held}, "WAIT_OUTCOME_KIND_FIRST_SUCCEEDED", ok, exitOK},
		{[]string{"wait", held, "--timeout", waitTimeout.String()}, "WAIT_OUTCOME_KIND_TIMED_OUT", "", exitTimeout},
	}
	for _, tc := range cases {
		r := c.run(append(withTask(tc.args...), "--json")...)
		var o struct {
			Kind      string            `json:"kind"`
			DecidedBy string            `json:"decided_by"`
			Jobs      []json.RawMessage `json:"jobs"`
		}
		if err := json.Unmarshal([]byte(r.stdout), &o); err != nil || r.code != tc.code || r.stderr != "" {
			t.Errorf("%v: %+v (%v)", tc.args, r, err)
			continue
		}
		if o.Kind != tc.kind || o.DecidedBy != tc.by || len(o.Jobs) == 0 {
			t.Errorf("%v: outcome %+v, want %s decided by %q", tc.args, o, tc.kind, tc.by)
		}
	}
	c.world.Gates.Release("g")
}

func TestAnArgumentOfTheJobIsNotAskingForJSON(t *testing.T) {
	c := newCLI(t)
	r := c.run("wait", "--no-such-flag", "--", "--json")
	if json.Valid([]byte(r.stderr)) {
		t.Fatalf("stderr = %q, want the plain error", r.stderr)
	}
}

func TestSubmitCheckAndMeasureRunTheTasksOwnCommands(t *testing.T) {
	c := newCLI(t)
	c.world.Tasks.Put("/repo", full, tasktest.Scripted("kv", []string{"print", "checked", ";", "exit", "0"}, []string{"emit", `{"ops": 7.5}`}))

	r := c.run(withTask("submit", "check", "HEAD", "--wait")...)
	if r.code != exitOK || !strings.Contains(r.stdout, "\tsucceeded") || !strings.Contains(r.stdout, "\tcheck") {
		t.Fatalf("submit check = %+v", r)
	}
	r = c.run(withTask("submit", "measure", "HEAD", "--samples", "3", "--wait")...)
	if r.code != exitOK || !strings.Contains(r.stdout, "measure (3 samples)") || !strings.Contains(r.stdout, "succeeded") {
		t.Fatalf("submit measure = %+v", r)
	}
	// The same measure again is answered from the saved samples, and says so.
	r = c.run(withTask("submit", "measure", "HEAD", "--samples", "3")...)
	if r.code != exitOK || !strings.Contains(r.stdout, "(reused a saved result)") {
		t.Fatalf("second submit measure = %+v", r)
	}
	// --no-cache runs it again.
	r = c.run(withTask("submit", "measure", "HEAD", "--samples", "3", "--no-cache", "--wait")...)
	if r.code != exitOK || strings.Contains(r.stdout, "reused") {
		t.Fatalf("no-cache submit measure = %+v", r)
	}
	if n := countRuns(c.world.Env, "emit"); n != 6 {
		t.Errorf("the benchmark ran %d times, want 3 and then 3 more for --no-cache", n)
	}
}

func TestSubmitOfATaskTheCommitLacksFailsAndQueuesNothing(t *testing.T) {
	c := newCLI(t)
	r := c.run(withTask("submit", "check", "HEAD")...)
	if r.code != exitInvalidInput || !strings.Contains(r.stderr, "is not a commit") {
		t.Fatalf("submit check = %+v", r)
	}
	if r := c.run(withTask("jobs")...); r.code != exitOK || strings.TrimSpace(r.stdout) != "" {
		t.Fatalf("jobs = %+v", r)
	}
}

func countRuns(env *fakeenv.Environment, word string) int {
	n := 0
	for _, x := range env.Runs() {
		if len(x.Argv) > 0 && x.Argv[0] == word {
			n++
		}
	}
	return n
}

func TestEnvironmentVariablesFillTheCallContext(t *testing.T) {
	c := newCLI(t)
	c.env["VIBESYS_TASK"] = "kv"
	c.env["VIBESYS_RUN"] = "run-7"
	c.env["VIBESYS_AGENT"] = "agent-3"
	r := c.run("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--note", "n", "--", "exit", "0")
	if r.code != exitOK {
		t.Fatalf("submit = %+v", r)
	}
}

// A placement inherited from VIBESYS_PLACEMENT is every submit's default for its docker jobs, so a
// host job is not refused for it, and records none.
func TestAHostJobIgnoresTheInheritedPlacement(t *testing.T) {
	c := newCLI(t)
	c.env["VIBESYS_PLACEMENT"] = "local"
	r := c.run(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--json", "--", "exit", "0")...)
	if r.code != exitOK || strings.Contains(r.stdout, `"placement"`) {
		t.Fatalf("submit = %+v; want a host job with no placement", r)
	}
}

func TestDaemonStatusStopRestart(t *testing.T) {
	c := newCLI(t)
	if r := c.run("daemon", "status"); r.code != exitNoAnswer || r.stdout != "not running\n" {
		t.Fatalf("status with no daemon = %+v", r)
	}
	if r := c.run("daemon", "stop"); r.code != exitOK || r.stdout != "not running\n" {
		t.Fatalf("stop with no daemon = %+v", r)
	}
	if c.starter.Calls() != 0 {
		t.Fatal("status or stop started a daemon")
	}
	c.starter.Inherited = daemon.Inherited{
		Environ:           []string{"PATH=/usr/bin", "VIBESYS_SPARK_HOST=host-a", "VIBESYS_API_TOKEN=t0ken", "AWS_SECRET=s3cret", "TMPDIR=/scratch"},
		CheckoutDirectory: "/scratch",
	}
	c.submit("exit", "0")
	r := c.run("daemon", "status")
	if r.code != exitOK || !strings.HasPrefix(r.stdout, "running\t") {
		t.Fatalf("status = %+v", r)
	}
	// The first line counts all of the daemon's work, the submissions it has not answered included.
	if first, _, _ := strings.Cut(r.stdout, "\n"); !strings.HasSuffix(first, "\tsubmitting 0") {
		t.Errorf("status first line = %q, want it to end with the submissions in progress", first)
	}
	// What a job inherits: every name, and a value only where it is safe to show.
	wantLines := "checkouts\t/scratch\nenv\tAWS_SECRET\nenv\tPATH=/usr/bin\nenv\tTMPDIR=/scratch\nenv\tVIBESYS_API_TOKEN\nenv\tVIBESYS_SPARK_HOST=host-a\n"
	if _, rest, _ := strings.Cut(r.stdout, "\n"); rest != wantLines {
		t.Errorf("status after its first line =\n%s\nwant\n%s", rest, wantLines)
	}
	j := c.run("daemon", "status", "--json")
	if j.code != exitOK || !strings.Contains(j.stdout, `"checkout_directory":"/scratch"`) ||
		!strings.Contains(j.stdout, `"VIBESYS_SPARK_HOST"`) || strings.Contains(j.stdout, "s3cret") || strings.Contains(j.stdout, "t0ken") {
		t.Errorf("status --json = %+v", j)
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
	if r := c.run("daemon", "status"); r.code != exitNoAnswer {
		t.Fatalf("status after stop = %+v", r)
	}
	if r := c.run("daemon", "install"); r.code != exitInvalidInput || !strings.Contains(r.stderr, "not available") {
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

// scopeAt is the scope of a task in a repository, as the daemon resolves one.
func scopeAt(repo, task string) core.Scope {
	s, err := core.ResolvedScope(core.Repository(repo), core.TaskName(task))
	if err != nil {
		panic(err)
	}
	return s
}

// Every command that takes the context of a call takes all of it, --placement included, from its
// flags as from the environment, so one context line serves every command: a command with no use
// for a flag, such as --placement outside submit, ignores it.
func TestEveryCommandTakesTheWholeContext(t *testing.T) {
	c := newCLI(t)
	job := c.submit("true")
	context := []string{"--run", "run-a", "--harness", "h", "--agent", "a", "--placement", "p"}
	for _, line := range [][]string{
		{"wait", job}, {"jobs"}, {"logs", job}, {"show", "HEAD"}, {"compare", "HEAD"}, {"result"}, {"note", job, "hi"}, {"cancel", job},
		{"frontier", "list"}, {"plan", "list"}, {"daemon", "status"}, {"submit", "check", "HEAD"},
	} {
		r := c.run(append(withTask(line...), context...)...)
		if strings.Contains(r.stderr, "not defined") {
			t.Errorf("%v refused the context: %q", line, r.stderr)
		}
	}
	c.env["VIBESYS_PLACEMENT"] = "p"
	if r := c.run(withTask("jobs")...); r.code != exitOK {
		t.Errorf("jobs with VIBESYS_PLACEMENT set: %+v", r)
	}
}

// A command job whose argv carries an inline script takes one short line in jobs and in submit's
// own output; --json still carries the whole argv.
func TestJobsPrintsALongArgvOnOneShortLine(t *testing.T) {
	c := newCLI(t)
	script := strings.Repeat("import os, sys  # a script passed inline as one argument\n", 40) + strings.Repeat("x", 500)
	r := c.run(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--", "print", script, ";", "exit", "0")...)
	if r.code != exitOK {
		t.Fatalf("submit = %+v", r)
	}
	id, _, _ := strings.Cut(r.stdout, "\t")
	for _, out := range []string{r.stdout, c.run(withTask("jobs")...).stdout} {
		var line string
		for _, l := range strings.Split(out, "\n") {
			if strings.HasPrefix(l, id+"\t") {
				line = l
			}
		}
		if line == "" || !strings.Contains(line, "40 more lines, full argv in --json") || utf8.RuneCountInString(line) > 2*commandWidth {
			t.Fatalf("job line = %q in %q; want one short line that says what it left out", line, out)
		}
	}
	if j := c.run(withTask("jobs", "--json")...); !strings.Contains(j.stdout, strings.Repeat("x", 500)) {
		t.Fatalf("jobs --json lost the argv: %q", j.stdout)
	}
}

// Any argv prints on one line of bounded length, and an argv that already fits prints as it is.
func TestCommandSummaryIsOneBoundedLine(t *testing.T) {
	property := func(argv []string) bool {
		got := commandSummary(argv)
		joined := strings.Join(argv, " ")
		fits := !strings.Contains(joined, "\n") && utf8.RuneCountInString(joined) <= commandWidth
		return !strings.Contains(got, "\n") &&
			utf8.RuneCountInString(got) <= commandWidth+len(" … (99999 more lines, full argv in --json)") &&
			(got == joined) == fits
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 2000}); err != nil {
		t.Fatal(err)
	}
}

// A command job's line names the image it runs in; a job stored before the image was named shows
// the one its candidate setup implies.
func TestAJobsLineNamesItsImage(t *testing.T) {
	build := &cliv1.CandidateBuild{Dockerfile: "serve/Dockerfile", Context: "serve"}
	serve := &cliv1.CandidateServe{Argv: []string{"serve"}, Ready: "/health"}
	for _, c := range []struct {
		name    string
		command *cliv1.CommandSettings
		want    string
	}{
		{"harness", &cliv1.CommandSettings{Image: cliv1.CommandImage_COMMAND_IMAGE_HARNESS}, "[harness]"},
		{"candidate", &cliv1.CommandSettings{Image: cliv1.CommandImage_COMMAND_IMAGE_CANDIDATE}, "[candidate]"},
		{"old, in the candidate's image", &cliv1.CommandSettings{Candidate: &cliv1.CandidateSetup{Build: build}}, "[candidate]"},
		{"old profile, beside the server", &cliv1.CommandSettings{Candidate: &cliv1.CandidateSetup{Build: build, Serve: serve}}, "[harness]"},
		{"old, of a task of one image", &cliv1.CommandSettings{}, "[harness]"},
	} {
		c.command.Argv = []string{"load"}
		got := describe(&cliv1.Job{Id: "j1", Spec: &cliv1.JobSpec{Settings: &cliv1.JobSpec_Command{Command: c.command}}})
		if !strings.Contains(got, "\t"+c.want+" load") {
			t.Errorf("%s: %q, want %q", c.name, got, c.want)
		}
	}
}
