package production

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

const (
	plainTool   = "/opt/a/vibesys-cli"
	wrappedTool = "/opt/wrapper/vibesys-cli-sandboxed"
	otherTool   = "/opt/b/vibesys-cli"
)

func newJobToolWorld() *jobToolWorld {
	return newWorld(map[string]worldTool{
		plainTool:   {Build: "build-a", Protocol: RequiredJobToolProtocol},
		wrappedTool: {Build: "build-a", Sandbox: true, Protocol: RequiredJobToolProtocol},
		otherTool:   {Build: "build-b", Protocol: RequiredJobToolProtocol},
	})
}

func hostEnv(env map[string]string) []string {
	var out []string
	for _, name := range slices.Sorted(maps.Keys(env)) {
		out = append(out, name+"="+env[name])
	}
	return out
}

func bounded(t *testing.T) context.Context {
	t.Helper()
	// The World never waits: this deadline is what the ensurer requires, and nothing gets near it.
	ctx, cancel := context.WithDeadline(context.Background(), time.Unix(1<<40, 0))
	t.Cleanup(cancel)
	return ctx
}

// chooseDaemon is what a host does when a run starts, in the host's own environment.
func chooseDaemon(ctx context.Context, w *jobToolWorld, tool string, host map[string]string) (core.JobToolEndpoint, error) {
	answer, err := ExecEnsurer{Endpoint: core.JobToolEndpoint{Executable: tool}, Runner: w, Environ: func() []string { return hostEnv(host) }}.Answer(ctx)
	if err != nil {
		return core.JobToolEndpoint{}, err
	}
	return core.JobToolEndpoint{Executable: tool, RuntimeDirectory: answer.GetRuntimeDirectory()}, nil
}

// asAgent is the environment an agent's command runs in: what the session holder does with what the
// host sends (driver.py, over), over what the operator's login gives.
func asAgent(t *testing.T, operator map[string]string, endpoint core.JobToolEndpoint) []string {
	t.Helper()
	start := sessionStart("agent-1", core.RoleLead, "/repo")
	start.JobTool = endpoint
	sent, err := agentEnvironment(start)
	if err != nil {
		t.Fatal(err)
	}
	env := maps.Clone(operator)
	for _, name := range sent.GetUnset() {
		delete(env, name)
	}
	maps.Copy(env, sent.GetSet())
	return hostEnv(env)
}

// agentCommand is the command an agent of a run runs against the daemon: a measure of the baseline,
// which says it tests no plan, as vibesysd requires of a measure from a run. The world's tools read
// none of its words; they stand for any command that needs a daemon.
var agentCommand = []string{"submit", "measure", "HEAD", "--no-plan"}

// runAgentCommand is an agent's command: exit status, and what it says on failure.
func runAgentCommand(ctx context.Context, w *jobToolWorld, tool string, env []string) (bool, string) {
	_, stderr, err := w.Run(ctx, tool, agentCommand, env)
	return err == nil, string(stderr)
}

func runningDirs(w *jobToolWorld) []string {
	var dirs []string
	for _, d := range w.Daemons() {
		dirs = append(dirs, d.Dir)
	}
	return dirs
}

// The named worlds: whatever the host finds when a run starts, it either ends with one daemon it can
// name, or it refuses with the tool's own words, and an agent of that run meets the same daemon.
func TestAHostFindsOrRefusesEachDaemonItCanMeet(t *testing.T) {
	const rt = "/run/user/7"
	const dir = rt + "/vibesys-cli"
	host := map[string]string{"HOME": "/home/u", "XDG_RUNTIME_DIR": rt}
	for _, tc := range []struct {
		name    string
		tool    string
		setup   func(*testing.T, *jobToolWorld)
		refused string
		// replaced says the daemon that ran before the start is not the one that runs after it.
		replaced bool
	}{
		{name: "daemon absent", tool: plainTool},
		{name: "daemon stale: it died and left its socket", tool: plainTool, setup: func(t *testing.T, w *jobToolWorld) {
			startedBy(t, w, plainTool, host)
			w.Kill(dir)
		}},
		{name: "same build running", tool: plainTool, setup: func(t *testing.T, w *jobToolWorld) { startedBy(t, w, plainTool, host) }},
		{name: "different build, idle, is replaced", tool: plainTool, replaced: true, setup: func(t *testing.T, w *jobToolWorld) { startedBy(t, w, otherTool, host) }},
		{name: "different build, busy, refuses", tool: plainTool, refused: "has 2 jobs", setup: func(t *testing.T, w *jobToolWorld) {
			startedBy(t, w, otherTool, host)
			w.Daemon(dir).Jobs = 2
		}},
		{name: "unreachable", tool: plainTool, refused: "does not answer", setup: func(t *testing.T, w *jobToolWorld) { w.SetUnreachable(dir, true) }},
		{name: "sandbox wrapper", tool: wrappedTool},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx := bounded(t)
			w := newJobToolWorld()
			if tc.setup != nil {
				tc.setup(t, w)
			}
			before := w.Daemon(dir)
			endpoint, err := chooseDaemon(ctx, w, tc.tool, host)
			if tc.refused != "" {
				if err == nil || !strings.Contains(err.Error(), tc.refused) {
					t.Fatalf("the start got %v, %v, want a refusal saying %q", endpoint, err, tc.refused)
				}
				if got := w.Daemon(dir); got != before {
					t.Fatalf("the refused start changed the daemon: %v", got)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if endpoint.RuntimeDirectory != dir || endpoint.Executable != tc.tool {
				t.Fatalf("the endpoint is %+v", endpoint)
			}
			d := w.Daemon(dir)
			if d == nil || (before != nil && (d == before) == tc.replaced) {
				t.Fatalf("before %+v, after %+v, replaced %v", before, d, tc.replaced)
			}
			if ok, why := runAgentCommand(ctx, w, tc.tool, asAgent(t, map[string]string{"HOME": "/home/u", "XDG_RUNTIME_DIR": "/run/elsewhere"}, endpoint)); !ok {
				t.Fatalf("the agent: %s", why)
			}
			if !slices.Equal(runningDirs(w), []string{dir}) || w.Daemon(dir) != d {
				t.Fatalf("after the agent's command the daemons are %v", runningDirs(w))
			}
		})
	}
}

func startedBy(t *testing.T, w *jobToolWorld, tool string, env map[string]string) {
	t.Helper()
	if _, _, err := w.Run(context.Background(), tool, []string{"daemon", "ensure", "--json"}, hostEnv(env)); err != nil {
		t.Fatal(err)
	}
}

// An agent that cannot reach its daemon says so and exits with no answer: it does not fall back to a
// daemon of its own, in any of the ways it could.
func TestAnAgentThatCannotReachItsDaemonStartsNoOther(t *testing.T) {
	ctx := bounded(t)
	const dir = "/run/user/7/vibesys-cli"
	host := map[string]string{"HOME": "/home/u", "XDG_RUNTIME_DIR": "/run/user/7", "XDG_STATE_HOME": "/state/host"}
	operator := map[string]string{"HOME": "/home/u", "XDG_RUNTIME_DIR": "/run/elsewhere", "XDG_STATE_HOME": "/state/elsewhere"}
	for name, fault := range map[string]func(*jobToolWorld){
		"the daemon does not answer":             func(w *jobToolWorld) { w.SetUnreachable(dir, true) },
		"the runtime directory went away":        func(w *jobToolWorld) { w.Wipe(dir) },
		"another build with jobs took the place": nil,
	} {
		t.Run(name, func(t *testing.T) {
			w := newJobToolWorld()
			endpoint, err := chooseDaemon(ctx, w, plainTool, host)
			if err != nil {
				t.Fatal(err)
			}
			if fault != nil {
				fault(w)
			} else {
				startedBy(t, w, otherTool, host)
				w.Daemon(dir).Jobs = 1
			}
			before := runningDirs(w)
			ok, why := runAgentCommand(ctx, w, plainTool, asAgent(t, operator, endpoint))
			if ok || why == "" {
				t.Fatalf("the agent's command = %v, %q; want no answer and a message", ok, why)
			}
			if got := runningDirs(w); !slices.Equal(got, before) {
				t.Fatalf("the daemons went from %v to %v", before, got)
			}
			if slices.Contains(w.Stores(), "/state/elsewhere") {
				t.Fatalf("the agent's own state home holds a store: %v", w.Stores())
			}
		})
	}
}

// someOperator is any environment an operator may have: a repository with its own XDG names set, or a
// profile that moves HOME, in any combination, with values the tool takes and values it ignores.
func someOperator(r *rand.Rand) map[string]string {
	paths := []string{"", "/", "/x", "/y/z", "relative", "./dot", "/run/user/7", "/home/u/.local/state"}
	pick := func() string { return paths[r.IntN(len(paths))] }
	env := map[string]string{"HOME": "/home/" + string(rune('a'+r.IntN(3))), "PATH": "/usr/bin", "OPENAI_LIKE_CREDENTIAL": "kept"}
	for _, name := range []string{"XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"} {
		if value := pick(); value != "" {
			env[name] = value
		}
	}
	if r.IntN(4) == 0 {
		env["VIBESYS_CLI_RUNTIME_DIR"] = "/operators/own/rt"
	}
	return env
}

// For any environment the operator has, an agent's vibesys-cli reaches the host's chosen daemon and
// only that, over that daemon's store, also when it has to start it again; and HOME and every XDG name
// are what the operator's are, so the agent's credentials and git configuration resolve as for a
// person.
func TestForAnyOperatorEnvironmentAnAgentReachesTheChosenDaemon(t *testing.T) {
	ctx := bounded(t)
	r := rand.New(rand.NewPCG(29, 31))
	placeName := envcodec.NameOf(&cliv1.JobToolPlace{}, "runtime_directory")
	for round := range 500 {
		w := newJobToolWorld()
		tool := []string{plainTool, wrappedTool}[r.IntN(2)]
		host := map[string]string{"HOME": "/home/host", "XDG_RUNTIME_DIR": "/run/user/7", "XDG_STATE_HOME": "/state/host"}
		if r.IntN(2) == 0 {
			delete(host, "XDG_RUNTIME_DIR")
		}
		if r.IntN(2) == 0 {
			delete(host, "XDG_STATE_HOME")
		}
		endpoint, err := chooseDaemon(ctx, w, tool, host)
		if err != nil {
			t.Fatalf("round %d: %v", round, err)
		}
		chosen := w.Daemon(endpoint.RuntimeDirectory)
		operator := someOperator(r)
		env := envcodec.FromEnviron(asAgent(t, operator, endpoint))
		for name, value := range operator {
			if (name == "HOME" || strings.HasPrefix(name, "XDG_")) && env(name) != value {
				t.Fatalf("round %d: %s is %q, the operator's is %q", round, name, env(name), value)
			}
		}
		if env(placeName) != endpoint.RuntimeDirectory {
			t.Fatalf("round %d: the agent's %s is %q", round, placeName, env(placeName))
		}
		agent := asAgent(t, operator, endpoint)
		if ok, why := runAgentCommand(ctx, w, tool, agent); !ok {
			t.Fatalf("round %d: %s", round, why)
		}
		if w.Daemon(endpoint.RuntimeDirectory) != chosen || len(w.Daemons()) != 1 {
			t.Fatalf("round %d: operator %v reached %v, the host chose %v", round, operator, runningDirs(w), endpoint.RuntimeDirectory)
		}
		w.Kill(endpoint.RuntimeDirectory)
		if ok, why := runAgentCommand(ctx, w, tool, agent); !ok {
			t.Fatalf("round %d after the daemon died: %s", round, why)
		}
		if again := w.Daemon(endpoint.RuntimeDirectory); again == nil || again.StateHome != chosen.StateHome || len(w.Daemons()) != 1 {
			t.Fatalf("round %d: the agent brought back %+v, the host's was %+v", round, again, chosen)
		}
		if got := w.Stores(); len(got) != 1 || got[0] != chosen.StateHome {
			t.Fatalf("round %d: the stores are %v, the host's is %s", round, got, chosen.StateHome)
		}
	}
}

// Whatever a start holds, the agent's environment names no place of the operator's: it sets and unsets
// only the names the messages declare. This is what keeps HOME out of it by construction.
func TestTheAgentsEnvironmentNamesOnlyWhatTheMessagesDeclare(t *testing.T) {
	r := rand.New(rand.NewPCG(37, 41))
	declared := slices.Concat(names(t, &cliv1.AgentContext{}), names(t, &cliv1.SkillEnvironment{}), names(t, &cliv1.JobToolPlace{}))
	for range 200 {
		start := sessionStart("agent-1", core.RoleLead, "/repo")
		start.JobTool = core.JobToolEndpoint{Executable: maybePath(r), RuntimeDirectory: maybePath(r)}
		sent, err := agentEnvironment(start)
		if err != nil {
			t.Fatal(err)
		}
		for _, name := range slices.Concat(slices.Collect(maps.Keys(sent.GetSet())), sent.GetUnset()) {
			if !slices.Contains(declared, name) {
				t.Fatalf("%s is in the agent's environment: %v", name, sent)
			}
		}
	}
}

// A tool that is not the one a host can use, because it is older or something else entirely, is
// refused saying so, and never read as a daemon with no place.
func TestAToolThatAnswersNoEndpointIsNotUsed(t *testing.T) {
	ctx := bounded(t)
	for name, stdout := range map[string]string{
		"an older tool prints its status": `{"versions":{"binaryVersion":"0.9","protocolVersion":3},"pid":41}`,
		"not json":                        "ready\tpid 41\n",
		"nothing":                         "",
		"relative socket":                 `{"socket":"daemon.sock","runtimeDirectory":"/run/x"}`,
		"no directory":                    `{"socket":"/run/x/daemon.sock"}`,
	} {
		t.Run(name, func(t *testing.T) {
			e := ExecEnsurer{Endpoint: core.JobToolEndpoint{Executable: plainTool}, Runner: scriptedRunner{stdout: stdout}, Environ: func() []string { return nil }}
			_, err := e.Ensure(ctx)
			var unreadable *clidaemon.Unreadable
			if !errors.As(err, &unreadable) || unreadable.Reason != core.CLINotInstalled || !strings.Contains(unreadable.Detail, plainTool) {
				t.Fatalf("%v", err)
			}
		})
	}
}

// The tool's failure reaches whoever reads the view in the tool's words, whether it is the typed
// Failure a --json command prints or whatever a wrapper in front of it said.
func TestATypedFailureIsReadAndAnythingElseIsQuoted(t *testing.T) {
	ctx := bounded(t)
	for stderr, want := range map[string]string{
		`{"kind":"FAILURE_KIND_DAEMON_OTHER_BUILD","message":"a daemon of another build has 3 jobs"}`: "a daemon of another build has 3 jobs",
		"sh: 1: exec: gone: not found\n": "sh: 1: exec: gone: not found",
	} {
		e := ExecEnsurer{Endpoint: core.JobToolEndpoint{Executable: plainTool}, Runner: scriptedRunner{stderr: stderr, err: errors.New("exit status 4")}, Environ: func() []string { return nil }}
		_, err := clidaemon.Ensure(ctx, e, core.NoDaemon)
		var unreadable *clidaemon.Unreadable
		if !errors.As(err, &unreadable) || unreadable.Reason != core.StartFailed || !strings.Contains(unreadable.Detail, want) {
			t.Fatalf("%q: %v", stderr, err)
		}
	}
}

type scriptedRunner struct {
	stdout, stderr string
	err            error
}

func (s scriptedRunner) Run(context.Context, string, []string, []string) ([]byte, []byte, error) {
	return []byte(s.stdout), []byte(s.stderr), s.err
}

// Whatever the host's environment holds, the tool runs in it with only the place laid over: what the
// host records wins over any value it inherited, and with none recorded the tool chooses.
func TestTheEnsureEnvironmentChangesOnlyThePlace(t *testing.T) {
	r := rand.New(rand.NewPCG(43, 47))
	placeName := envcodec.NameOf(&cliv1.JobToolPlace{}, "runtime_directory")
	for range 300 {
		host := someOperator(r)
		endpoint := core.JobToolEndpoint{Executable: plainTool, RuntimeDirectory: maybePath(r)}
		got, err := ensureEnvironment(hostEnv(host), endpoint)
		if err != nil {
			t.Fatal(err)
		}
		env := envcodec.FromEnviron(got)
		if env(placeName) != endpoint.RuntimeDirectory {
			t.Fatalf("the place is %q, want %q", env(placeName), endpoint.RuntimeDirectory)
		}
		for name, value := range host {
			if name != placeName && env(name) != value {
				t.Fatalf("%s changed to %q from %q", name, env(name), value)
			}
		}
	}
}

// jobToolSim is a run's job tool over a world things happen to, in the holders_sim style: a seed makes
// the whole history, and the follower runs on this goroutine, since every event happens where the
// follower would have slept.
type jobToolSim struct {
	t        *testing.T
	r        *rand.Rand
	w        *jobToolWorld
	endpoint core.JobToolEndpoint
	operator map[string]string
	host     map[string]string
	trace    []string
	failed   *clidaemon.Unreadable
	read     int
}

func (s *jobToolSim) note(format string, args ...any) {
	s.trace = append(s.trace, fmt.Sprintf(format, args...))
}

func (s *jobToolSim) fatalf(format string, args ...any) {
	s.t.Helper()
	s.t.Fatalf("%s\nhistory:\n  %s", fmt.Sprintf(format, args...), strings.Join(s.trace, "\n  "))
}

// event makes one thing happen and checks what must hold after any of them.
func (s *jobToolSim) event() {
	dir := s.endpoint.RuntimeDirectory
	d := s.w.Daemon(dir)
	switch s.r.IntN(8) {
	case 0:
		s.note("the daemon dies")
		s.w.Kill(dir)
	case 1:
		s.note("the runtime directory goes away")
		s.w.Wipe(dir)
	case 2:
		s.note("the daemon stops answering")
		s.w.SetUnreachable(dir, true)
	case 3:
		s.note("the daemon answers again")
		s.w.SetUnreachable(dir, false)
	case 4:
		if d != nil {
			d.Jobs = s.r.IntN(3)
			s.note("the daemon has %d jobs", d.Jobs)
		}
	case 5:
		s.note("another build's command, in the operator's plain shell of this directory")
		_, _, _ = s.w.Run(context.Background(), otherTool, []string{"daemon", "ensure", "--json"}, hostEnv(s.host))
	case 6:
		// A run's agent is not told to wait: it gets an answer, or exit 4 and a message.
		ok, why := runAgentCommand(context.Background(), s.w, s.endpoint.Executable, asAgent(s.t, s.operator, s.endpoint))
		s.note("an agent's command: %v %s", ok, why)
		if !ok && why == "" {
			s.fatalf("an agent's command failed saying nothing")
		}
		if ok && s.w.Daemon(dir) == nil {
			s.fatalf("an agent's command worked with no daemon of the run")
		}
	case 7:
		s.note("nothing happens")
	}
	for _, other := range s.w.Daemons() {
		if other.Dir != dir {
			s.fatalf("a second daemon runs, in %s", other.Dir)
		}
	}
	for _, home := range s.w.Stores() {
		if home != s.w.Places(dir) && s.w.Places(dir) != "" {
			s.fatalf("a store in %s, and the directory records %s", home, s.w.Places(dir))
		}
	}
}

func TestSimulatedJobToolHistories(t *testing.T) {
	for seed := uint64(1); seed <= 200; seed++ {
		runJobToolHistory(t, seed)
	}
}

func runJobToolHistory(t *testing.T, seed uint64) {
	t.Helper()
	r := rand.New(rand.NewPCG(seed, 53))
	w := newJobToolWorld()
	host := map[string]string{"HOME": "/home/host", "XDG_RUNTIME_DIR": "/run/user/7", "XDG_STATE_HOME": "/state/host"}
	s := &jobToolSim{t: t, r: r, w: w, host: host, operator: map[string]string{"HOME": "/home/host", "XDG_RUNTIME_DIR": "/run/elsewhere", "XDG_STATE_HOME": "/state/elsewhere"}}
	ctx, cancel := context.WithDeadline(context.Background(), time.Unix(1<<40, 0))
	defer cancel()
	endpoint, err := chooseDaemon(ctx, w, plainTool, host)
	if err != nil {
		t.Fatal(err)
	}
	s.endpoint = endpoint
	steps := 20 + r.IntN(40)
	follower := clidaemon.Follower{
		Connect: w.Connect,
		Ensurer: ExecEnsurer{Endpoint: endpoint, Runner: w, Environ: func() []string { return hostEnv(host) }},
		Bound:   func(c context.Context) (context.Context, context.CancelFunc) { return c, func() {} },
		Changed: func(facts []*cliv1.TaskFact) { s.read = len(facts); s.failed = nil },
		Failed:  func(u *clidaemon.Unreadable) { s.failed = u; s.note("the follower: %v %s", u.Reason, u.Detail) },
		Sleep: func(context.Context, time.Duration) error {
			steps--
			switch {
			case steps > 0:
				s.event()
			case steps == 0:
				// The world heals: whatever happened, the next look finds a daemon of the run.
				s.note("the world heals")
				w.SetUnreachable(endpoint.RuntimeDirectory, false)
				if d := w.Daemon(endpoint.RuntimeDirectory); d != nil {
					d.Jobs = 0
				}
				if _, _, err := w.Run(ctx, plainTool, agentCommand, asAgent(t, s.operator, endpoint)); err != nil && w.Places(endpoint.RuntimeDirectory) != "" {
					s.fatalf("a healed world refused an agent: %v", err)
				}
			default:
				cancel()
				return context.Canceled
			}
			return nil
		},
	}
	err = follower.Follow(ctx, &cliv1.WatchTaskRequest{Context: &cliv1.CallContext{Repository: "/repo", Task: "task"}})
	if !errors.Is(err, context.Canceled) {
		s.fatalf("seed %d: the follower stopped by itself: %v", seed, err)
	}
	// Every look was at the run's directory, and only that.
	for _, dir := range w.Ensured {
		if dir != endpoint.RuntimeDirectory {
			s.fatalf("seed %d: an ensure asked about %s", seed, dir)
		}
	}
	if w.Daemon(endpoint.RuntimeDirectory) == nil {
		s.fatalf("seed %d: no daemon after the world healed", seed)
	}
}
