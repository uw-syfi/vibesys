package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/filestores"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/shorttmp"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/host"
)

// The end-to-end tests run the real daemon and the real supervisors as processes, started the way
// a user's commands start them, and kill them at chosen points. Only the environment is a fake:
// the commands are scripts, and their gates are named pipes, so a script waits for the test
// without polling or sleeping.
//
// The test binary is those processes. Started with VIBESYS_E2E_ROLE set it is the daemon or a
// supervisor, composed as production composes them but over the fake environment, and otherwise
// it runs the tests.
const (
	roleVar       = "VIBESYS_E2E_ROLE"
	rtVar         = "VIBESYS_E2E_RT"
	indexVar      = "VIBESYS_E2E_INDEX"
	gatesVar      = "VIBESYS_E2E_GATES"
	runDirVar     = "VIBESYS_E2E_RUNDIR"
	windowVar     = "VIBESYS_E2E_WINDOW"
	quickRetryVar = "VIBESYS_E2E_QUICK_RETRY"
	// machineVar is the machine directory of the test binary as the shipped tool, for a test of
	// daemons that are not sandboxes: they hold a machine of the test's own, and never the user's.
	machineVar = "VIBESYS_E2E_MACHINE"
	roleCLI    = "cli"
)

// The windows of a launch a test can stop it in.
const (
	// windowSpawned kills the daemon with the supervisor parked.
	windowSpawned = "spawned"
	// windowStarted kills the daemon once the command was told to start.
	windowStarted = "started"
	// windowSupervisor kills the supervisor once it answered and before its start is sent, and the
	// daemon goes on.
	windowSupervisor = "supervisor"
)

func TestMain(m *testing.M) {
	switch os.Getenv(roleVar) {
	case "daemon":
		os.Exit(e2eDaemon())
	case "supervisor":
		os.Exit(e2eSupervisor())
	case "ready":
		fmt.Print(readyPipe() != nil)
		os.Exit(0)
	case "":
		// The tool, mounted in a task's container, probing a candidate's server.
		if len(os.Args) > 1 && os.Args[1] == "probe-ready" {
			os.Exit(run(os.Args[1:], realDeps(os.Stdout, os.Stderr)))
		}
	case roleCLI:
		// The test binary as the shipped tool: its daemon and supervisor processes are this binary
		// again, composed by internal/production as the release is, over the real machine.
		d := realDeps(os.Stdout, os.Stderr)
		if dir := os.Getenv(machineVar); dir != "" {
			d.process.MachineDir = dir
			d.options = d.process.ClientOptions
		}
		os.Exit(run(os.Args[1:], d))
	}
	// The tests start from none of the operator's names, whatever shell runs them.
	scrubOperatorEnvironment()
	os.Exit(shorttmp.Run(m))
}

type anyFS struct{}

func (anyFS) IsNetwork(string) (bool, error) { return false, nil }

func e2eEnvironments() fakeenv.Environments {
	gates := fakeenv.NewFIFOGates(os.Getenv(gatesVar))
	return fakeenv.Environments{core.EnvironmentHost: fakeenv.New(core.EnvironmentHost, gates)}
}

func e2eDaemon() int {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM)
	defer stop()
	self, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	dir, err := runtimedir.Open(os.Getenv(rtVar), os.Getuid(), anyFS{})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	index, err := filestore.OpenIndex(os.Getenv(indexVar))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	// A test that has a live store holder on purpose does not want the minute of a real daemon.
	var retry app.Retry
	var slowPeriod time.Duration
	if os.Getenv(quickRetryVar) != "" {
		retry = app.Retry{Attempts: 2, First: time.Millisecond, Longest: time.Millisecond}
		// A job in doubt is looked at again every slow period, which is 30 seconds by default.
		// What the test checks holds at any pace; a short one only brings the second look sooner.
		slowPeriod = 50 * time.Millisecond
	}
	supervisors := host.New(host.Config{
		Root: dir.Supervisors(), Records: host.RecordsDir(os.Getenv(indexVar), "e2e", dir.Path()), BootID: runtimedir.BootID,
		Command: func(runDir string) *exec.Cmd {
			cmd := exec.Command(self, "-test.run=^$")
			cmd.Env = append(os.Environ(), roleVar+"=supervisor", runDirVar+"="+runDir)
			return cmd
		},
		BinaryVersion: "e2e", Timers: clock.System{},
	})
	err = daemon.Run(ctx, daemon.Config{
		Dir: dir, Version: "e2e", Clock: clock.System{}, Ready: os.NewFile(runtimedir.ReadyFD, "ready"), Retry: retry, SlowPeriod: slowPeriod,
		// Output is written to the store when it has waited this long, a second by default, and every
		// test waits for some. When it is written changes no result.
		LogBatchDelay: 10 * time.Millisecond,
		Logf:          func(format string, args ...any) { fmt.Fprintf(os.Stderr, "daemon: "+format+"\n", args...) },
		Runtime: daemon.Runtime{
			Stores:       filestores.New(index, app.Rules(5), filestore.Owner{Host: "e2e", Dir: dir.Path(), PID: os.Getpid()}),
			Environments: e2eEnvironments(),
			Supervisors:  dyingSupervisors{Supervisors: supervisors, host: supervisors, marker: os.Getenv(windowVar)},
			Repositories: ownRepository{},
		},
	})
	if errors.Is(err, daemon.ErrAlreadyRunning) {
		return 0
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

// ownRepository resolves a directory to itself: the e2e repositories are plain directories, each
// its own repository, and the daemon process cannot see the test's gitrepo.Fake.
type ownRepository struct{}

func (ownRepository) Resolve(_ context.Context, dir string) (core.Repository, error) {
	return core.Repository(filepath.Clean(dir)), nil
}

// dyingSupervisors stops a launch in the window a test wrote into the marker, once, so a kill lands
// where the test says and not where the timing happens to put it.
type dyingSupervisors struct {
	supervisor.Supervisors
	host   *host.Host
	marker string
}

func die() {
	syscall.Kill(os.Getpid(), syscall.SIGKILL)
	select {}
}

// Spawn implements supervisor.Supervisors.
func (d dyingSupervisors) Spawn(ctx context.Context, l lifecycle.Launch) (supervisor.Handle, error) {
	window, _ := os.ReadFile(d.marker)
	if len(window) > 0 {
		os.Remove(d.marker)
	}
	h, err := d.Supervisors.Spawn(ctx, l)
	if err != nil {
		return nil, err
	}
	switch string(window) {
	case windowSpawned:
		die()
	case windowStarted:
		return dyingHandle{h}, nil
	case windowSupervisor:
		pid, err := d.host.PID(l.Ref())
		if err != nil {
			return nil, err
		}
		syscall.Kill(pid, syscall.SIGKILL)
		// The daemon goes on once the supervisor is proved dead, so it always tells a dead one to
		// start: its command never started, and the job runs again. A look before the kernel freed
		// the lock would mark the start sent first, and the job would be lost.
		for {
			run, err := d.host.Probe(ctx, l.Ref())
			if err != nil {
				return nil, err
			}
			if _, dead := run.(supervisor.Dead); dead {
				break
			}
			runtime.Gosched()
		}
	}
	return h, nil
}

// dyingHandle ends the daemon once the command was told to start.
type dyingHandle struct{ supervisor.Handle }

func (d dyingHandle) Start(ctx context.Context) error {
	err := d.Handle.Start(ctx)
	die()
	return err
}

func e2eSupervisor() int {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM)
	defer stop()
	err := host.Serve(ctx, os.Getenv(runDirVar), host.Deps{
		Environments: e2eEnvironments(), Checkouts: &fakeenv.Checkouts{}, BinaryVersion: "e2e",
	}, os.NewFile(host.ReadyFD, "ready"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

// A daemon started by hand has no readiness pipe, and the daemon must see that it has none: a nil
// file in an interface is not a nil interface, and the write to it would stop the daemon.
func TestAProcessStartedByHandHasNoReadinessPipe(t *testing.T) {
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	defer writer.Close()
	for want, files := range map[string][]*os.File{"false": nil, "true": {writer}} {
		cmd := exec.Command(self, "-test.run=^$")
		cmd.Env = append(os.Environ(), roleVar+"=ready")
		cmd.ExtraFiles = files
		out, err := cmd.Output()
		if err != nil {
			t.Fatal(err)
		}
		if string(out) != want {
			t.Errorf("with %d extra files, a readiness pipe = %s, want %s", len(files), out, want)
		}
	}
}

// e2e is a user's machine: a repository, a runtime directory, and the processes that come and go.
type e2e struct {
	t      *testing.T
	root   string
	repo   string
	dir    runtimedir.Dir
	gates  *fakeenv.FIFOGates
	git    *gitrepo.Fake
	starts atomic.Int64
	// quickRetry starts daemons that give up on a held store at once and look again at what they could
	// not settle at a short pace.
	quickRetry bool
}

func newE2E(t *testing.T) *e2e {
	t.Helper()
	root, err := os.MkdirTemp("", "vse")
	if err != nil {
		t.Fatal(err)
	}
	root, _ = filepath.EvalSymlinks(root)
	e := &e2e{t: t, root: root, repo: filepath.Join(root, "repo"), git: gitrepo.NewFake()}
	for _, d := range []string{e.repo, filepath.Join(root, "gates"), filepath.Join(root, "index")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	e.dir = mustOpen(t, filepath.Join(root, "rt"))
	e.gates = fakeenv.NewFIFOGates(filepath.Join(root, "gates"))
	e.git.AddRepo(e.repo, map[string]core.Commit{"HEAD": full})
	t.Cleanup(func() {
		// Whatever a failed test left running ends here, so no process outlives the test.
		e.killEverything()
		os.RemoveAll(root)
	})
	return e
}

func mustOpen(t *testing.T, path string) runtimedir.Dir {
	t.Helper()
	dir, err := runtimedir.Open(path, os.Getuid(), anyFS{})
	if err != nil {
		t.Fatal(err)
	}
	return dir
}

// elsewhere is the same user and repository under another runtime directory, as a daemon has when
// XDG_RUNTIME_DIR differs. It cannot see the supervisors of the first.
func (e *e2e) elsewhere() *e2e {
	e.t.Helper()
	o := &e2e{t: e.t, root: e.root, repo: e.repo, gates: e.gates, git: e.git, dir: mustOpen(e.t, filepath.Join(e.root, "rt2"))}
	e.t.Cleanup(o.killEverything)
	return o
}

func (e *e2e) windowFile() string { return filepath.Join(e.root, "window") }

func (e *e2e) options() client.Options {
	self, err := os.Executable()
	if err != nil {
		e.t.Fatal(err)
	}
	return client.Options{
		Rendezvous: client.LocalRendezvous{Dir: e.dir}, Version: "e2e",
		Starter: client.ExecStarter{Dir: e.dir, Command: func() *exec.Cmd {
			e.starts.Add(1)
			cmd := exec.Command(self, "-test.run=^$")
			cmd.Env = append(os.Environ(), roleVar+"=daemon", rtVar+"="+e.dir.Path(),
				indexVar+"="+filepath.Join(e.root, "index"), gatesVar+"="+e.gates.Dir(), windowVar+"="+e.windowFile())
			if e.quickRetry {
				cmd.Env = append(cmd.Env, quickRetryVar+"=1")
			}
			return cmd
		}},
	}
}

// run is one invocation of the command line, as a user's shell would make it, in the repository.
func (e *e2e) run(args ...string) result {
	e.t.Helper()
	var stdout, stderr strings.Builder
	d := deps{
		stdout: &stdout, stderr: &stderr,
		getenv:  func(string) string { return "" },
		cwd:     e.repo,
		git:     e.git,
		options: func() (client.Options, error) { return e.options(), nil },
	}
	code := run(args, d)
	return result{code, stdout.String(), stderr.String()}
}

func (e *e2e) submit(argv ...string) string {
	e.t.Helper()
	r := e.run(append(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--"), argv...)...)
	if r.code != exitOK {
		e.t.Fatalf("submit: exit %d: %s", r.code, r.stderr)
	}
	return strings.Fields(r.stdout)[0]
}

func (e *e2e) release(gate string) {
	e.t.Helper()
	if err := e.gates.Release(gate); err != nil {
		e.t.Fatal(err)
	}
}

// waitFor blocks on the job through the command line and returns what it printed and its exit code.
func (e *e2e) wait(ids ...string) result {
	e.t.Helper()
	return e.run(withTask(append([]string{"wait", "--timeout", hangBound.String()}, ids...)...)...)
}

func (e *e2e) logs(id string) string {
	e.t.Helper()
	r := e.run(withTask("logs", id)...)
	if r.code != exitOK {
		e.t.Fatalf("logs: exit %d: %s", r.code, r.stderr)
	}
	return r.stdout
}

var errSeen = errors.New("seen")

// hangBound only turns a hang into a failure. A wait that reaches it found a bug, so it is far
// longer than any wait takes on a loaded machine and no result depends on it.
const hangBound = 5 * time.Minute

// awaitOutput blocks until the job's stored output holds the text. It follows the output stream,
// so it returns when the text arrives and not after a time.
func (e *e2e) awaitOutput(id, text string) {
	e.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), hangBound)
	defer cancel()
	c, err := client.Connect(ctx, e.options())
	if err != nil {
		e.t.Fatal(err)
	}
	defer c.Close()
	var seen strings.Builder
	err = c.Logs(ctx, &cliv1.ReadLogsRequest{
		Context: &cliv1.CallContext{Repository: e.repo, Task: "kv"}, JobId: id, Follow: true,
	}, func(r *cliv1.ReadLogsResponse) error {
		seen.Write(r.GetData())
		if strings.Contains(seen.String(), text) {
			return errSeen
		}
		return nil
	})
	if !errors.Is(err, errSeen) {
		e.t.Fatalf("waiting for %q in the output of %s: %v (saw %q)", text, id, err, seen.String())
	}
}

// expectSupervisorGone checks a job that ended left no supervisor behind. Seeing the job end does
// not show that: the daemon records the end first and lets the supervisor go after, so that an
// outcome is never gone before it is safe. The next job does show it, because a job that ended
// holds the machine until its run is removed. So this starts a job and looks once that job runs.
func (e *e2e) expectSupervisorGone(id string) {
	e.t.Helper()
	next := e.submit("print", "next", ";", "hold", "next", ";", "exit", "0")
	e.awaitOutput(next, "next\n")
	left := e.supervisors()
	if len(left) != 1 || string(left[0].Ref.Job) != next {
		e.t.Fatalf("supervisors while the job after %s runs = %v, want only the one of %s", id, left, next)
	}
}

// killAndAwaitDeath kills the process and returns once it is gone. It holds a connection to the
// process's socket before the kill, and the connection ends when the process's descriptors close,
// which is when it is dead. Nothing polls and nothing sleeps.
func (e *e2e) killAndAwaitDeath(socket string, pid int) {
	e.t.Helper()
	conn, err := net.Dial("unix", socket)
	if err != nil {
		e.t.Fatalf("connecting to %s before killing %d: %v", socket, pid, err)
	}
	defer conn.Close()
	e.killAndAwaitEnd(conn, pid)
}

// killAndAwaitEnd kills the process and returns when the connection to it ends.
func (e *e2e) killAndAwaitEnd(conn net.Conn, pid int) {
	e.t.Helper()
	if err := syscall.Kill(pid, syscall.SIGKILL); err != nil {
		e.t.Fatalf("kill %d: %v", pid, err)
	}
	if err := conn.SetDeadline(clock.System{}.Now().Add(hangBound)); err != nil {
		e.t.Fatal(err)
	}
	if _, err := io.Copy(io.Discard, conn); err != nil && !errors.Is(err, syscall.ECONNRESET) {
		e.t.Fatalf("waiting for %d to die: %v", pid, err)
	}
}

// awaitCommandEnd blocks until the command of the job has ended, by asking its supervisor, which
// needs no daemon.
func (e *e2e) awaitCommandEnd(id string) {
	e.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), hangBound)
	defer cancel()
	for _, info := range e.supervisors() {
		if string(info.Ref.Job) != id {
			continue
		}
		found, err := host.New(host.Config{Root: e.dir.Supervisors(), BinaryVersion: "e2e", Timers: clock.System{}}).Probe(ctx, info.Ref)
		run, alive := found.(supervisor.Alive)
		if err != nil || !alive {
			e.t.Fatalf("Probe of the supervisor of %s = %+v, %v", id, found, err)
		}
		defer run.Handle.Close()
		if _, err := run.Handle.Follow(ctx, 1<<62, func([]byte) error { return nil }); err != nil {
			e.t.Fatal(err)
		}
		return
	}
	e.t.Fatalf("job %s has no supervisor", id)
}

func (e *e2e) daemonPID() int {
	e.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), hangBound)
	defer cancel()
	c, err := client.Dial(ctx, e.options())
	if err != nil {
		e.t.Fatalf("no daemon: %v", err)
	}
	defer c.Close()
	st, err := c.Status(ctx)
	if err != nil {
		e.t.Fatal(err)
	}
	return int(st.GetPid())
}

func (e *e2e) killDaemon() {
	e.t.Helper()
	e.killAndAwaitDeath(e.dir.Socket(), e.daemonPID())
}

func (e *e2e) supervisors() []host.Info {
	e.t.Helper()
	infos, err := host.Inspect(e.dir.Supervisors())
	if err != nil {
		e.t.Fatal(err)
	}
	return infos
}

// supervisorsAmong returns those of the given supervisors that are still listed, by their job.
func (e *e2e) supervisorsAmong(of []host.Info) []host.Info {
	e.t.Helper()
	var left []host.Info
	for _, now := range e.supervisors() {
		for _, was := range of {
			if now.Ref == was.Ref {
				left = append(left, now)
			}
		}
	}
	return left
}

func (e *e2e) killSupervisor(info host.Info) {
	e.t.Helper()
	if info.PID == 0 {
		e.t.Fatalf("supervisor %s has not written its id", info.Ref)
	}
	e.killAndAwaitDeath(info.Socket, info.PID)
}

func (e *e2e) killEverything() {
	if c, err := client.Dial(context.Background(), client.Options{Rendezvous: client.LocalRendezvous{Dir: e.dir}, Version: "e2e"}); err == nil {
		if st, err := c.Status(context.Background()); err == nil {
			syscall.Kill(int(st.GetPid()), syscall.SIGKILL)
		}
		c.Close()
	}
	if infos, err := host.Inspect(e.dir.Supervisors()); err == nil {
		for _, i := range infos {
			if i.PID > 0 {
				syscall.Kill(i.PID, syscall.SIGKILL)
			}
		}
	}
}

// expectLostOutput checks the output of a lost job is what ran before the loss and then the one
// line in which the daemon says the job was lost, and nothing the command would have written
// had it gone on.
func (e *e2e) expectLostOutput(id, ran string) {
	e.t.Helper()
	out := e.logs(id)
	rest, found := strings.CutPrefix(out, ran)
	if !found || strings.Count(rest, "\n") != 1 || !strings.Contains(rest, "is lost and is not run again") {
		e.t.Fatalf("output of the lost job = %q, want %q and one line saying the job is lost", out, ran)
	}
}

func (e *e2e) job(id string) string {
	e.t.Helper()
	r := e.run(withTask("jobs", "--json")...)
	if r.code != exitOK {
		e.t.Fatalf("jobs: %+v", r)
	}
	for _, line := range strings.Split(r.stdout, "\n") {
		if strings.Contains(line, `"id":"`+id+`"`) {
			return line
		}
	}
	e.t.Fatalf("job %s is not listed: %s", id, r.stdout)
	return ""
}

func TestE2EAJobRunsToTheEndInTheDaemonsProcess(t *testing.T) {
	e := newE2E(t)
	id := e.submit("print", "hello", ";", "exit", "0")
	if r := e.wait(id); r.code != exitOK || !strings.Contains(r.stdout, "succeeded (exit 0)") {
		t.Fatalf("wait = %+v", r)
	}
	if out := e.logs(id); out != "hello\n" {
		t.Fatalf("logs = %q", out)
	}
	if pid := e.daemonPID(); pid == os.Getpid() {
		t.Fatal("the daemon runs in the test's process")
	}
	if got := e.starts.Load(); got != 1 {
		t.Fatalf("daemon starts = %d, want 1", got)
	}
	e.expectSupervisorGone(id)
}

// Commands that find no daemon at the same moment start one between them.
func TestE2EConcurrentCommandsStartOneDaemonProcess(t *testing.T) {
	e := newE2E(t)
	var wg sync.WaitGroup
	for range 6 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if r := e.run(withTask("jobs")...); r.code != exitOK {
				t.Errorf("jobs = %+v", r)
			}
		}()
	}
	wg.Wait()
	if got := e.starts.Load(); got != 1 {
		t.Fatalf("daemon starts = %d, want 1", got)
	}
}

// The daemon dies while a job runs. The next command starts another, which finds the supervisor,
// follows the job where it left off, and the output has each line once.
func TestE2EADaemonKilledMidJobIsReplacedAndFollowsTheJobOn(t *testing.T) {
	e := newE2E(t)
	id := e.submit("print", "A", ";", "hold", "g", ";", "print", "A2", ";", "exit", "0")
	e.awaitOutput(id, "A\n")
	before := e.supervisors()
	e.killDaemon()

	// The new daemon is started by the command that needs it.
	waited := make(chan result, 1)
	go func() { waited <- e.wait(id) }()
	e.release("g")
	if r := <-waited; r.code != exitOK || !strings.Contains(r.stdout, "succeeded (exit 0)") {
		t.Fatalf("wait = %+v", r)
	}
	if out := e.logs(id); out != "A\nA2\n" {
		t.Fatalf("output = %q, want each line exactly once", out)
	}
	if len(before) != 1 {
		t.Fatalf("supervisors before = %v, want one", before)
	}
	if got := e.starts.Load(); got != 2 {
		t.Fatalf("daemon starts = %d, want 2", got)
	}
}

// A job can finish while no daemon runs. Its supervisor keeps the outcome and the output until a
// daemon comes and collects them.
func TestE2EAJobThatFinishesWhileTheDaemonIsDownIsCollected(t *testing.T) {
	e := newE2E(t)
	id := e.submit("print", "A", ";", "hold", "g", ";", "print", "A2", ";", "exit", "0")
	e.awaitOutput(id, "A\n")
	e.killDaemon()
	e.release("g")
	e.awaitCommandEnd(id)

	if r := e.wait(id); r.code != exitOK || !strings.Contains(r.stdout, "succeeded (exit 0)") {
		t.Fatalf("wait = %+v", r)
	}
	if out := e.logs(id); out != "A\nA2\n" {
		t.Fatalf("output = %q", out)
	}
}

// A store has one daemon, wherever else one is started. A daemon under another runtime directory
// cannot see the supervisors, so it is refused while the first lives and, while a job runs, after
// that too. It may take the store over once the first stopped and nothing runs.
func TestE2EAStoreHasOneDaemonAcrossRuntimeDirectories(t *testing.T) {
	e := newE2E(t)
	other := e.elsewhere()
	id := e.submit("print", "A", ";", "hold", "g", ";", "print", "A2", ";", "exit", "0")
	e.awaitOutput(id, "A\n")

	r := other.run(append(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--"), "exit", "0")...)
	if r.code == exitOK || !strings.Contains(r.stderr, "in use by process") || !strings.Contains(r.stderr, e.dir.Path()) {
		t.Fatalf("a second daemon's submit = %+v", r)
	}
	e.killDaemon()
	// Whether the kernel freed the store yet only decides which refusal this is.
	r = other.run(append(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--"), "exit", "0")...)
	if r.code == exitOK || !strings.Contains(r.stderr, "process") {
		t.Fatalf("a submit elsewhere with a job running here = %+v", r)
	}

	e.release("g")
	if r := e.wait(id); r.code != exitOK {
		t.Fatalf("wait = %+v", r)
	}
	if r := e.run("daemon", "stop"); r.code != exitOK {
		t.Fatalf("daemon stop = %+v", r)
	}
	next := other.submit("print", "moved", ";", "exit", "0")
	if r := other.wait(next); r.code != exitOK {
		t.Fatalf("wait after the store moved = %+v", r)
	}
}

// The instance lock is only the first line. With it deleted, a second daemon comes up in the same
// runtime directory while the first lives, and the store still has one daemon.
func TestE2EADeletedInstanceLockStillLeavesTheStoreOneDaemon(t *testing.T) {
	e := newE2E(t)
	e.quickRetry = true
	id := e.submit("print", "A", ";", "hold", "g", ";", "print", "A2", ";", "exit", "0")
	e.awaitOutput(id, "A\n")
	first := e.daemonPID()
	conn, err := net.Dial("unix", e.dir.Socket())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	for _, f := range []string{e.dir.DaemonLock(), e.dir.Socket()} {
		if err := os.Remove(f); err != nil {
			t.Fatal(err)
		}
	}

	r := e.run(append(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--"), "exit", "0")...)
	if r.code == exitOK || !strings.Contains(r.stderr, fmt.Sprintf("in use by process %d", first)) {
		t.Fatalf("a submit to the second daemon = %+v", r)
	}

	e.killAndAwaitEnd(conn, first)
	e.quickRetry = false
	if r := e.run("daemon", "restart"); r.code != exitOK {
		t.Fatalf("daemon restart = %+v", r)
	}
	e.release("g")
	if r := e.wait(id); r.code != exitOK {
		t.Fatalf("wait = %+v", r)
	}
	if out := e.logs(id); out != "A\nA2\n" {
		t.Fatalf("output = %q", out)
	}
}

// A repository on a disk that is unmounted leaves an empty directory at its path. That costs
// nothing: no store is made there, its job is not lost, and what the job did meanwhile is there
// when the disk comes back.
func TestE2EARepositoryThatIsAwayKeepsItsJob(t *testing.T) {
	e := newE2E(t)
	id := e.submit("print", "A", ";", "hold", "g", ";", "print", "A2", ";", "exit", "0")
	e.awaitOutput(id, "A\n")
	e.killDaemon()
	away := e.repo + ".away"
	if err := os.Rename(e.repo, away); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(e.repo, 0o755); err != nil {
		t.Fatal(err)
	}
	if r := e.run("daemon", "restart"); r.code != exitOK {
		t.Fatalf("daemon restart while away = %+v", r)
	}
	e.release("g")
	e.awaitCommandEnd(id)
	if r := e.run("daemon", "restart"); r.code != exitOK {
		t.Fatalf("daemon restart while away = %+v", r)
	}
	if made, err := os.ReadDir(e.repo); err != nil || len(made) != 0 {
		t.Fatalf("the mount point holds %v, %v; want nothing", made, err)
	}
	if left := e.supervisors(); len(left) != 1 {
		t.Fatalf("supervisors while away = %v, want the one that keeps the result", left)
	}

	if err := os.Remove(e.repo); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(away, e.repo); err != nil {
		t.Fatal(err)
	}
	if r := e.run("daemon", "restart"); r.code != exitOK {
		t.Fatalf("daemon restart after the return = %+v", r)
	}
	if r := e.wait(id); r.code != exitOK || !strings.Contains(r.stdout, "succeeded (exit 0)") {
		t.Fatalf("wait = %+v", r)
	}
	if out := e.logs(id); out != "A\nA2\n" {
		t.Fatalf("output = %q", out)
	}
}

// A kill inside a launch, with real processes: the job after one that ends is launched with the
// test's window armed, and it comes to a clean end from there. The daemon is killed by itself at
// the very point the test names, so no timing decides which window this was.
func TestE2EAKillInsideALaunch(t *testing.T) {
	for _, tc := range []struct{ window, ended string }{
		{windowSpawned, "succeeded (exit 0)"},
		{windowStarted, "succeeded (exit 0)"},
		{windowSupervisor, "succeeded (exit 0)"},
	} {
		t.Run(tc.window, func(t *testing.T) {
			e := newE2E(t)
			first := e.submit("print", "A", ";", "hold", "g", ";", "exit", "0")
			e.awaitOutput(first, "A\n")
			next := e.submit("print", "B", ";", "exit", "0")
			if err := os.WriteFile(e.windowFile(), []byte(tc.window), 0o600); err != nil {
				t.Fatal(err)
			}
			e.release("g")
			r := e.wait(first, next)
			if !strings.Contains(r.stdout, tc.ended) {
				t.Fatalf("wait = %+v, want a job %s", r, tc.ended)
			}
			// The supervisor window kills only the supervisor, before its start: the daemon goes on
			// and runs the job again.
			if n := e.starts.Load(); tc.window != windowSupervisor && n != 2 {
				t.Fatalf("%d daemons started; the kill should have made it 2", n)
			}
			want := "B\n"
			if tc.window == windowSupervisor {
				// The log keeps the line that says why the job runs again, then the command's
				// output, once.
				want = lifecycle.CutLine(1, core.CutBeforeStart{By: core.CutSupervisorDied}) + "\n" + want
			}
			if out := e.logs(next); out != want {
				t.Fatalf("output = %q, want %q: the command ran other than once", out, want)
			}
			e.expectSupervisorGone(next)
		})
	}
}

// Stopping the daemon on purpose stops no job.
func TestE2EStoppingTheDaemonStopsNoJob(t *testing.T) {
	e := newE2E(t)
	id := e.submit("print", "A", ";", "hold", "g", ";", "print", "A2", ";", "exit", "0")
	e.awaitOutput(id, "A\n")
	if r := e.run("daemon", "stop"); r.code != exitOK || r.stdout != "stopped\n" {
		t.Fatalf("daemon stop = %+v", r)
	}
	if r := e.run("daemon", "restart"); r.code != exitOK {
		t.Fatalf("daemon restart = %+v", r)
	}
	e.release("g")
	if r := e.wait(id); r.code != exitOK {
		t.Fatalf("wait = %+v", r)
	}
	if out := e.logs(id); out != "A\nA2\n" {
		t.Fatalf("output = %q", out)
	}
}

// The job's supervisor dies while the daemon is up: the job is lost, once, and never run again.
func TestE2EASupervisorKilledWhileTheDaemonIsUpLosesTheJob(t *testing.T) {
	e := newE2E(t)
	id := e.submit("print", "A", ";", "hold", "g", ";", "print", "A2", ";", "exit", "0")
	e.awaitOutput(id, "A\n")
	sups := e.supervisors()
	if len(sups) != 1 {
		t.Fatalf("supervisors = %v, want one", sups)
	}
	e.killSupervisor(sups[0])

	r := e.wait(id)
	if r.code != exitFailed || !strings.Contains(r.stdout, "lost") {
		t.Fatalf("wait = %+v, want the job lost", r)
	}
	// Not retried: the output is what it was, and a new daemon decides the same.
	e.release("g")
	e.killDaemon()
	if r := e.wait(id); r.code != exitFailed || !strings.Contains(r.stdout, "lost") {
		t.Fatalf("wait after a restart = %+v", r)
	}
	e.expectLostOutput(id, "A\n")
}

// The supervisor dies while no daemon runs: the next daemon finds nothing to follow and records
// the job as lost.
func TestE2EASupervisorKilledWhileTheDaemonIsDownLosesTheJob(t *testing.T) {
	e := newE2E(t)
	id := e.submit("print", "A", ";", "hold", "g", ";", "exit", "0")
	e.awaitOutput(id, "A\n")
	e.killDaemon()
	e.killSupervisor(e.supervisors()[0])

	if r := e.wait(id); r.code != exitFailed || !strings.Contains(r.stdout, "lost") {
		t.Fatalf("wait = %+v, want the job lost", r)
	}
}

// Jobs queued behind a running one survive the daemon's death and run in order, one at a time.
func TestE2EQueuedJobsSurviveTheDaemonAndRunInOrder(t *testing.T) {
	e := newE2E(t)
	first := e.submit("print", "first", ";", "hold", "g", ";", "exit", "0")
	second := e.submit("print", "second", ";", "exit", "0")
	third := e.submit("print", "third", ";", "exit", "0")
	e.awaitOutput(first, "first\n")
	e.killDaemon()
	e.release("g")

	if r := e.wait(first, second, third); r.code != exitOK {
		t.Fatalf("wait = %+v", r)
	}
	for id, want := range map[string]string{first: "first\n", second: "second\n", third: "third\n"} {
		if out := e.logs(id); out != want {
			t.Errorf("output of %s = %q, want %q", id, out, want)
		}
	}
}

// A machine restart kills the daemon and every supervisor. The job that ran is lost and not
// retried; the jobs that were queued are not lost and run.
func TestE2EAMachineRestartLosesTheRunningJobAndKeepsTheQueuedOnes(t *testing.T) {
	e := newE2E(t)
	running := e.submit("print", "ran", ";", "hold", "g", ";", "exit", "0")
	queued := e.submit("print", "queued", ";", "exit", "0")
	e.awaitOutput(running, "ran\n")
	e.killDaemon()
	e.killSupervisor(e.supervisors()[0])

	if r := e.wait(queued); r.code != exitOK {
		t.Fatalf("wait for the queued job = %+v", r)
	}
	if out := e.logs(queued); out != "queued\n" {
		t.Fatalf("output of the queued job = %q", out)
	}
	if r := e.wait(running); r.code != exitFailed || !strings.Contains(r.stdout, "lost") {
		t.Fatalf("wait for the running job = %+v, want lost", r)
	}
	e.expectLostOutput(running, "ran\n")
}

// Cancelling a running job ends its supervisor and tells every waiter why.
func TestE2ECancelEndsTheJobAndItsSupervisor(t *testing.T) {
	e := newE2E(t)
	id := e.submit("print", "A", ";", "hold", "g", ";", "print", "never", ";", "exit", "0")
	e.awaitOutput(id, "A\n")

	waited := make(chan result, 1)
	go func() { waited <- e.wait(id) }()
	r := e.run(withTask("cancel", id, "--reason", "not needed")...)
	if r.code != exitOK || !strings.Contains(r.stdout, "cancelled (not needed)") {
		t.Fatalf("cancel = %+v", r)
	}
	if w := <-waited; w.code != exitFailed || !strings.Contains(w.stdout, "cancelled (not needed)") {
		t.Fatalf("the waiter saw %+v", w)
	}
	if out := e.logs(id); out != "A\n" {
		t.Fatalf("output = %q", out)
	}
	e.expectSupervisorGone(id)
}

// otherRepository is a second repository of the same user, under the same runtime directory, so
// the same daemon serves both.
func (e *e2e) otherRepository() *e2e {
	e.t.Helper()
	o := &e2e{t: e.t, root: e.root, repo: filepath.Join(e.root, "repo-b"), dir: e.dir, gates: e.gates, git: e.git}
	if err := os.MkdirAll(o.repo, 0o755); err != nil {
		e.t.Fatal(err)
	}
	e.git.AddRepo(o.repo, map[string]core.Commit{"HEAD": full})
	return o
}

// waitThrough waits for the jobs as a caller that is told why a job waits and asks again would:
// a wait that ends with a stated reason is made again, up to the bound that only guards a hang.
func (e *e2e) waitThrough(ids ...string) result {
	e.t.Helper()
	deadline := clock.System{}.Now().Add(hangBound)
	for {
		r := e.wait(ids...)
		if r.code != exitNoAnswer || (clock.System{}).Now().After(deadline) {
			return r
		}
	}
}

// A repository that goes away under a live daemon stops nothing: its job runs on and is recorded
// whole when the repository is back, and the job of another repository runs in its turn.
func TestE2EARepositoryThatGoesAwayUnderALiveDaemonStopsNothing(t *testing.T) {
	e := newE2E(t)
	// The job in doubt is looked at again every slow period, and a real daemon's is 30 seconds.
	e.quickRetry = true
	b := e.otherRepository()
	id := e.submit("print", "A", ";", "hold", "g", ";", "print", "A2", ";", "exit", "0")
	e.awaitOutput(id, "A\n")
	next := b.submit("print", "B", ";", "exit", "0")
	away := e.repo + ".away"
	if err := os.Rename(e.repo, away); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(e.repo, 0o755); err != nil {
		t.Fatal(err)
	}
	e.release("g")
	if r := b.waitThrough(next); r.code != exitOK || !strings.Contains(r.stdout, "succeeded (exit 0)") {
		t.Fatalf("wait in the other repository = %+v", r)
	}
	if err := os.Remove(e.repo); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(away, e.repo); err != nil {
		t.Fatal(err)
	}
	if r := e.waitThrough(id); r.code != exitOK || !strings.Contains(r.stdout, "succeeded (exit 0)") {
		t.Fatalf("wait after the return = %+v", r)
	}
	if out := e.logs(id); out != "A\nA2\n" {
		t.Fatalf("output = %q", out)
	}
}

// P1-1 with real processes: a worktree removed while its job runs leaves a supervisor that keeps
// its result forever, and that never holds up another repository. `daemon forget` lets it go.
func TestE2EARepositoryGoneForGoodHoldsUpNoOtherAndIsForgotten(t *testing.T) {
	e := newE2E(t)
	b := e.otherRepository()
	id := e.submit("print", "A", ";", "hold", "g", ";", "exit", "0")
	e.awaitOutput(id, "A\n")
	e.killDaemon()
	if err := os.RemoveAll(e.repo); err != nil {
		t.Fatal(err)
	}
	e.release("g")
	e.awaitCommandEnd(id)
	// Only this repository's supervisor is in the runtime directory now: the daemon is dead and
	// nothing else was submitted. Its entry is the one the forget has to remove.
	mine := e.supervisors()
	if len(mine) != 1 {
		t.Fatalf("supervisors of the repository before the forget = %v, want one", mine)
	}

	next := b.submit("print", "B", ";", "exit", "0")
	if r := b.waitThrough(next); r.code != exitOK || !strings.Contains(r.stdout, "succeeded (exit 0)") {
		t.Fatalf("wait in the other repository = %+v", r)
	}
	if r := b.run("daemon", "forget", e.repo); r.code != exitOK || !strings.Contains(r.stdout, "task") {
		t.Fatalf("daemon forget = %+v", r)
	}
	// The listing is of the whole runtime directory, and the other repository's job is in it until
	// the daemon lets its supervisor go, which it does after recording the end that the wait above
	// saw (see expectSupervisorGone). That entry is going away on its own, with the forget not
	// involved, so only the forgotten repository's entry is looked at: the forget answers after
	// removing it.
	if left := e.supervisorsAmong(mine); len(left) != 0 {
		t.Fatalf("supervisors of the forgotten repository after the forget = %v, want none", left)
	}
	if r := b.run("daemon", "forget", e.repo); r.code == exitOK {
		t.Fatalf("a second forget = %+v, want it refused as unknown", r)
	}
}

// P2-4 with real processes: a runtime directory cleared at logout takes a live supervisor's socket
// and its proof of life with it. That proves nothing, so the next job of the store waits, with a
// reason, and is not started beside the command that still runs.
func TestE2EAClearedRuntimeDirectoryStartsNothingBesideALiveCommand(t *testing.T) {
	e := newE2E(t)
	id := e.submit("print", "A", ";", "hold", "g", ";", "exit", "0")
	e.awaitOutput(id, "A\n")
	live := e.supervisors()
	e.killDaemon()
	if err := os.RemoveAll(e.dir.Supervisors()); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(e.dir.Supervisors(), 0o700); err != nil {
		t.Fatal(err)
	}
	next := e.submit("print", "B", ";", "exit", "0")
	if r := e.wait(next); r.code != exitNoAnswer || !strings.Contains(r.stderr, id) {
		t.Fatalf("wait for the job after = %+v, want it told it waits for %s", r, id)
	}
	if out := e.logs(next); out != "" {
		t.Fatalf("the next job ran beside a live command: output %q", out)
	}
	// The command still runs, which is what made starting the next one wrong.
	for _, info := range live {
		if err := syscall.Kill(info.PID, 0); err != nil {
			t.Fatalf("supervisor %d: %v", info.PID, err)
		}
		syscall.Kill(info.PID, syscall.SIGKILL)
	}
}
