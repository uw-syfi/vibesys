package production

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// startSim is a whole daemon whose sessions are started by the real start protocol, over a world that
// can crash the host and kill the holder at each step of it.
type startSim struct {
	tb    testing.TB
	r     *daemontest.Rig
	world *holderWorld
}

func newStartSim(tb testing.TB) *startSim {
	tb.Helper()
	s := &startSim{tb: tb, r: daemontest.NewRig(tb), world: newHolderWorld()}
	const root = "/sessions"
	s.r.NewHost = func() daemon.Sidecars { return s.world.sidecars(root) }
	s.r.Restart(tb)
	return s
}

// restart starts a new host. Its own start asks the holders who they are, so it can crash in turn.
func (s *startSim) restart() {
	s.tb.Helper()
	for range 100 {
		if !s.crashed(func() { s.r.Restart(s.tb) }) {
			return
		}
	}
	s.tb.Fatal("the host never came back")
}

// crashed runs f and says whether the host crashed in it.
func (s *startSim) crashed(f func()) (crashed bool) {
	defer func() {
		if r := recover(); r != nil {
			if _, ok := r.(daemontest.Crash); !ok {
				panic(r)
			}
			crashed = true
		}
	}()
	f()
	return false
}

func (s *startSim) settle() {
	s.tb.Helper()
	for range 2000 {
		moved := false
		if s.crashed(func() {
			s.r.Hub.Flush()
			var err error
			moved, err = s.r.Daemon.Step(context.Background())
			if err != nil && !errors.Is(err, daemon.ErrExpired) {
				s.tb.Logf("step: %v", err)
				moved = true
			}
		}) {
			s.restart()
			continue
		}
		if !moved {
			return
		}
	}
	s.tb.Fatal("the daemon never settled")
}

// settleToEnd also lets the launch timer run, so a session that will never open has an end.
func (s *startSim) settleToEnd() {
	s.tb.Helper()
	s.settle()
	s.r.Clock.Advance(2 * core.LaunchTimeout)
	s.settle()
}

func (s *startSim) start() core.AgentID {
	s.tb.Helper()
	resp, err := s.r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: "c1", Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
		Repository: "/repo", Task: "task", FanoutLimit: 3, Brief: "go",
		Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}}})
	if err != nil {
		s.tb.Fatal(err)
	}
	run, ok := s.r.Daemon.State().Run(core.RunID(resp.GetReceipt().GetRunId()))
	if !ok {
		s.tb.Fatalf("no run in %v", resp)
	}
	return run.Lead
}

func (s *startSim) lead(id core.AgentID) core.Agent {
	s.tb.Helper()
	a, ok := s.r.Daemon.State().Agent(id)
	if !ok {
		s.tb.Fatalf("no agent %s", id)
	}
	return a
}

// A host that crashes at any one step of a start finds the session again: one agent was started, it
// is the first session, and it opened.
func TestAHostThatCrashesAtAnyStepOfAStartFindsTheSessionAgain(t *testing.T) {
	for _, step := range startSteps {
		t.Run(step, func(t *testing.T) {
			s := newStartSim(t)
			s.world.crashes[step] = true
			lead := s.start()
			s.settle()
			if s.world.crashes[step] {
				t.Fatalf("the start never reached %q", step)
			}
			a := s.lead(lead)
			if a.Status.Over() || a.Status == core.AgentLaunching || a.ResumeToken != "token" {
				t.Fatalf("lead %s, resume %q after a crash %s: want it open", a.Status, a.ResumeToken, step)
			}
			if a.Session != 1 || s.world.startedAgents() != 1 {
				t.Fatalf("session %d with %d agents started, want the first with one", a.Session, s.world.startedAgents())
			}
		})
	}
}

// Whatever crashes and deaths happen in whatever order, a start ends: a session open with one agent,
// or an end that says why. No session ever has two agents, and an agent never runs on beside another
// session of the same agent.
func TestEveryInterleavingOfCrashesAndDeathsEndsInOneOutcome(t *testing.T) {
	for seed := range uint64(300) {
		t.Run(fmt.Sprint(seed), func(t *testing.T) {
			r := rand.New(rand.NewPCG(seed, 0x51de))
			s := newStartSim(t)
			for _, step := range startSteps {
				switch r.IntN(4) {
				case 0:
					s.world.crashes[step] = true
				case 1:
					s.world.deaths[step] = true
				}
			}
			s.world.lateBinds = r.IntN(3)
			reasons := []pb.StartFailureReason{
				pb.StartFailureReason_START_FAILURE_REASON_UNSPECIFIED, pb.StartFailureReason_START_FAILURE_REASON_NOT_SIGNED_IN,
				pb.StartFailureReason_START_FAILURE_REASON_TOOLS_UNAVAILABLE,
			}
			if reason := reasons[r.IntN(len(reasons))]; reason != pb.StartFailureReason_START_FAILURE_REASON_UNSPECIFIED {
				s.world.outcome = func(*pb.OpenSessionRequest) []*pb.SessionEvent {
					return []*pb.SessionEvent{{Body: &pb.SessionEvent_Failed{Failed: &pb.SessionFailed{Detail: "could not start", StartFailureReason: reason}}}}
				}
			}
			lead := s.start()
			s.settleToEnd()
			a := s.lead(lead)
			switch {
			case a.Status == core.AgentLaunching:
				t.Fatalf("the lead is still launching: %+v", a)
			case a.Status.Over() && a.EndDetail == "":
				t.Fatalf("the lead ended with no reason: %+v", a)
			case !a.Status.Over() && a.ResumeToken == "":
				t.Fatalf("the lead is %s and never opened: %+v", a.Status, a)
			}
			for socket, n := range s.world.started {
				if n > 1 {
					t.Fatalf("%d agents were started for %s", n, socket)
				}
			}
			if live := s.world.liveAgents(); live > 1 {
				t.Fatalf("%d sessions of one agent have agents running", live)
			}
			if live := s.world.liveAgents(); live > 0 && a.Status.Over() {
				t.Fatalf("the lead ended (%s) and %d agents still run", a.Status, live)
			}
		})
	}
}

// A host that crashes after it started a process and before the process listens finds that process
// holding the session's lock, and waits for it. It does not start a second one in its place.
func TestAHostThatCrashesBetweenSpawnAndBindStartsNoSecondProcess(t *testing.T) {
	for late := 1; late <= 3; late++ {
		t.Run(fmt.Sprint(late, " dials find nothing"), func(t *testing.T) {
			s := newStartSim(t)
			s.world.crashes[stepSpawnAfter] = true
			s.world.lateBinds = late
			lead := s.start()
			s.settle()
			a := s.lead(lead)
			if a.Status.Over() || a.ResumeToken != "token" || a.Session != 1 {
				t.Fatalf("lead %s, session %d, resume %q: want the first session open", a.Status, a.Session, a.ResumeToken)
			}
			if s.world.spawns != 1 || s.world.startedAgents() != 1 {
				t.Fatalf("%d processes and %d agents were started, want one of each", s.world.spawns, s.world.startedAgents())
			}
		})
	}
}

// A second process at a place a process holds is refused, whatever the host believes.
func TestAProcessIsNotStartedAtAPlaceAProcessHolds(t *testing.T) {
	w := newHolderWorld()
	place := HolderPlace{Socket: "/s/sock", Directory: "/s"}
	if _, err := w.Spawn(place, []string{"/bin/holder"}, place.Bootstrap()); err != nil {
		t.Fatal(err)
	}
	if _, err := w.Spawn(place, []string{"/bin/holder"}, place.Bootstrap()); !errors.Is(err, ErrHolderLives) {
		t.Fatalf("got %v, want ErrHolderLives while the first has not bound", err)
	}
}

// A holder whose process is gone is found gone from its lock, at once: a name its socket left, which
// a call to it still reaches, is not taken for a holder, and a call that failed with Unavailable is
// not retried until a stream ends. The agent it left is ended, and the reason is named.
func TestADeadHoldersLockSaysSoWhateverItsSocketNameDoes(t *testing.T) {
	for _, call := range []string{"send", "follow", "events"} {
		t.Run(call, func(t *testing.T) {
			w := newHolderWorld()
			sidecars := w.sidecars("/sessions")
			start := sessionStart("agent-1", core.RoleLead, "/repo")
			held, err := sidecars.Launch(context.Background(), start)
			if err != nil {
				t.Fatal(err)
			}
			stream, err := held.Follow(context.Background(), 0)
			if err != nil {
				t.Fatal(err)
			}
			place := sidecars.place(start.Launch.Session)
			w.holders[place.Socket].dead = true // SIGKILLed: the lock is free, the socket name is still there.
			if !w.agents[place.Socket] {
				t.Fatal("the agent is not running before the holder died")
			}
			switch call {
			case "send":
				err = held.Send(context.Background(), &pb.SessionCommand{EffectId: "e1"})
			case "follow":
				_, err = held.Follow(context.Background(), 0)
			case "events":
				_, err = stream.Next()
			}
			var gone *daemon.GoneError
			if !errors.Is(err, daemon.ErrSidecarGone) || !errors.As(err, &gone) || !strings.Contains(gone.Reason, "no longer running") {
				t.Fatalf("got %v, want the session gone, with its reason", err)
			}
			if w.agents[place.Socket] {
				t.Fatal("the agent the holder left is still running")
			}
		})
	}
}

// A holder that lives is never written off for an error from a call to it.
func TestAHolderThatLivesIsNotWrittenOffForAFailedCall(t *testing.T) {
	w := newHolderWorld()
	sidecars := w.sidecars("/sessions")
	start := sessionStart("agent-1", core.RoleLead, "/repo")
	held, err := sidecars.Launch(context.Background(), start)
	if err != nil {
		t.Fatal(err)
	}
	place := sidecars.place(start.Launch.Session)
	w.holders[place.Socket].unreachable = true
	err = held.Send(context.Background(), &pb.SessionCommand{EffectId: "e1"})
	if err == nil || errors.Is(err, daemon.ErrSidecarGone) {
		t.Fatalf("got %v, want the call's own error", err)
	}
	if !w.agents[place.Socket] {
		t.Fatal("the agent of a holder that lives was ended")
	}
}

// A holder killed without warning ends its session lost, with the reason, and the agent it left is
// reaped: none runs on in the worktree of nothing. The lead goes on under a second session.
func TestAKilledHoldersSessionEndsLostAndNamedAndItsAgentIsReaped(t *testing.T) {
	s := newStartSim(t)
	lead := s.start()
	s.settle()
	if s.world.liveAgents() != 1 {
		t.Fatalf("%d agents run before the kill", s.world.liveAgents())
	}
	for _, h := range s.world.holders {
		h.dead = true
	}
	s.settle()
	a := s.lead(lead)
	if a.Session != 2 || a.Status.Over() {
		t.Fatalf("lead %s in session %d, want it live in the second", a.Status, a.Session)
	}
	if s.world.startedAgents() != 2 || s.world.liveAgents() != 1 {
		t.Fatalf("%d agents started and %d live, want the first one reaped", s.world.startedAgents(), s.world.liveAgents())
	}
	events, err := s.r.Store.EventsAfter(a.Run, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range events {
		if lost, ok := e.Event.Body.(core.Agent); ok && lost.Status == core.AgentLost {
			if !strings.Contains(lost.EndDetail, "no longer running") {
				t.Fatalf("the session was lost with %q, want the reason", lost.EndDetail)
			}
			return
		}
	}
	t.Fatal("no event says the first session was lost")
}

// A holder of another protocol ends in a named outcome, never a hang, and is not left running.
func TestAHolderOfAnotherProtocolEndsTheStartWithANamedReason(t *testing.T) {
	current := uint32(pb.SessionProtocolVersion_SESSION_PROTOCOL_VERSION_CURRENT)
	for name, arm := range map[string]func(*holderWorld){
		"newer":                func(w *holderWorld) { w.protocol = current + 1 },
		"older":                func(w *holderWorld) { w.protocol = current - 1 },
		"with no version word": func(w *holderWorld) { w.noHello = true },
	} {
		t.Run(name, func(t *testing.T) {
			s := newStartSim(t)
			arm(s.world)
			lead := s.start()
			s.settle()
			a := s.lead(lead)
			if !a.Status.Over() || a.StartFailure != core.StartIncompatibleVersions {
				t.Fatalf("lead %s with reason %q, want over with %q", a.Status, a.StartFailure, core.StartIncompatibleVersions)
			}
			if !strings.Contains(a.EndDetail, "cannot work") {
				t.Fatalf("end detail %q does not say what is wrong", a.EndDetail)
			}
			if s.world.startedAgents() != 0 || len(s.world.holders) != 0 {
				t.Fatalf("%d agents and %d holders outlived the refusal", s.world.startedAgents(), len(s.world.holders))
			}
		})
	}
}

// A host upgraded past the session it finds does not hang on it: that session is ended with the reason
// named, and the agent goes on under a new one of this host's protocol.
func TestAnUpgradedHostEndsTheSessionItCannotDriveAndResumesTheAgent(t *testing.T) {
	s := newStartSim(t)
	lead := s.start()
	s.settle()
	for _, h := range s.world.holders {
		h.protocol++ // the holder it left is of another release, whichever way round.
	}
	s.restart()
	s.settle()
	a := s.lead(lead)
	if a.Status == core.AgentLaunching || a.Status.Over() {
		t.Fatalf("lead %s (%s): want it resumed", a.Status, a.EndDetail)
	}
	if a.Session != 2 || s.world.startedAgents() != 2 || s.world.liveAgents() != 1 {
		t.Fatalf("session %d, %d started, %d live: want the second session the only one live", a.Session, s.world.startedAgents(), s.world.liveAgents())
	}
}

// Asking for a session again starts nothing, whichever host asks.
func TestARepeatedLaunchAnswersTheSameAndStartsNoSecondAgent(t *testing.T) {
	const root = "/sessions"
	w := newHolderWorld()
	start := sessionStart("agent-1", core.RoleLead, "/repo")
	for range 3 {
		if _, err := w.sidecars(root).Launch(context.Background(), start); err != nil {
			t.Fatal(err)
		}
	}
	if w.spawns != 1 || w.startedAgents() != 1 {
		t.Fatalf("%d spawns and %d agents, want one of each", w.spawns, w.startedAgents())
	}
}

// What answers on a session's socket for another session is never taken for it.
func TestAProcessHoldingAnotherSessionIsNotOpened(t *testing.T) {
	const root = "/sessions"
	w := newHolderWorld()
	sidecars := w.sidecars(root)
	start := sessionStart("agent-1", core.RoleLead, "/repo")
	place := sidecars.place(start.Launch.Session)
	if _, err := sidecars.Launch(context.Background(), start); err != nil {
		t.Fatal(err)
	}
	w.holders[place.Socket].opened.Session.AgentId = "agent-2"
	_, err := w.sidecars(root).Launch(context.Background(), start)
	var failed *daemon.LaunchError
	if !errors.As(err, &failed) || failed.Reason != core.StartOther || !strings.Contains(err.Error(), "agent-2") {
		t.Fatalf("got %v, want a refusal naming what answers", err)
	}
	if w.startedAgents() != 1 {
		t.Fatalf("%d agents were started", w.startedAgents())
	}
}

// A bad field is refused by its name before anything starts: no worktree, no process, no agent.
func TestABadFieldIsRefusedByNameBeforeAnythingStarts(t *testing.T) {
	good := func() daemon.SessionStart { return sessionStart("agent-1", core.RoleWorker, "/repo") }
	for _, bad := range []struct {
		name  string
		field string
		spoil func(*daemon.SessionStart, *ProcessSidecars)
	}{
		{"a relative repository as a lead's directory", "working_directory", func(s *daemon.SessionStart, _ *ProcessSidecars) {
			s.Launch.Role, s.Repository = core.RoleLead, "repo"
		}},
		{"no repository as a lead's directory", "working_directory", func(s *daemon.SessionStart, _ *ProcessSidecars) {
			s.Launch.Role, s.Repository = core.RoleLead, ""
		}},
		{"a relative agent root", "agent_data_directory", func(_ *daemon.SessionStart, p *ProcessSidecars) { p.Root = "relative" }},
		{"a relative marketplace", SettingSkills, func(_ *daemon.SessionStart, p *ProcessSidecars) { p.Parts = holderParts("skills") }},
		{"a relative tool server", "tools[0].argv", func(_ *daemon.SessionStart, p *ProcessSidecars) { p.Executable = "vibesys-host" }},
		{"a tool environment name that is no name", "tools[0].env", func(s *daemon.SessionStart, _ *ProcessSidecars) {
			s.Tools = daemon.LaunchEnv{"A=B": "x"}
		}},
		{"no harness", "harness", func(s *daemon.SessionStart, _ *ProcessSidecars) { s.Launch.Spec.Harness = "" }},
	} {
		t.Run(bad.name, func(t *testing.T) {
			w := newHolderWorld()
			sidecars := w.sidecars("/sessions")
			start := good()
			bad.spoil(&start, sidecars)
			_, err := sidecars.Launch(context.Background(), start)
			if err == nil || !strings.Contains(err.Error(), bad.field) {
				t.Fatalf("got %v, want a refusal naming %s", err, bad.field)
			}
			if w.spawns != 0 || w.worktrees != 0 || w.startedAgents() != 0 {
				t.Fatalf("%d spawns, %d worktrees, %d agents before a refusal", w.spawns, w.worktrees, w.startedAgents())
			}
		})
	}
}

// FuzzOpenSessionRequest holds what a launcher does with what it is given, whatever it is given: it
// starts one agent, or it refuses having started nothing.
func FuzzOpenSessionRequest(f *testing.F) {
	f.Add("/repo", "/skills", "/bin/vibesys-host", "TOKEN", "a-model", "/cli")
	f.Add("repo", "", "", "A=B", "", "cli")
	f.Add("", "/skills\x00", "/x", "1A", "\xff", "")
	f.Fuzz(func(t *testing.T, repository, marketplace, executable, envName, model, cli string) {
		w := newHolderWorld()
		sidecars := w.sidecars("/sessions")
		sidecars.Parts = holderParts(marketplace)
		sidecars.Executable = executable
		start := sessionStart("agent-1", core.RoleLead, repository)
		start.Tools = daemon.LaunchEnv{envName: "x"}
		start.Launch.Spec.Model = core.ModelName(model)
		start.Location.CLI = cli
		_, err := sidecars.Launch(context.Background(), start)
		if err == nil {
			if w.startedAgents() != 1 {
				t.Fatalf("%d agents were started for an accepted start", w.startedAgents())
			}
			return
		}
		if w.spawns != 0 || w.startedAgents() != 0 {
			t.Fatalf("%v, after %d spawns and %d agents", err, w.spawns, w.startedAgents())
		}
	})
}

// A process that cannot start is a failure the daemon can read, with what the shell knows.
func TestAProcessThatCannotStartIsReported(t *testing.T) {
	w := newHolderWorld()
	w.failSpawn = errors.New("no such program")
	_, err := w.sidecars("/sessions").Launch(context.Background(), sessionStart("agent-1", core.RoleLead, "/repo"))
	if err == nil || !strings.Contains(err.Error(), "no such program") {
		t.Fatalf("got %v", err)
	}
}

// conformanceScenario is the part of the corpus about the time before a session: the same files the
// Python implementation plays (orchestrator/sidecar/tests/test_conformance.py). Here the holder of the
// world plays them, so a fake a host is tested against is one that the corpus holds to.
type conformanceScenario struct {
	Name  string            `json:"name"`
	Steps []conformanceStep `json:"steps"`
}

type conformanceStep struct {
	Hello *struct {
		Client        json.RawMessage `json:"client"`
		Compatibility string          `json:"compatibility"`
		Session       *struct {
			AgentID string `json:"agentId"`
			Session uint32 `json:"session"`
		} `json:"session"`
	} `json:"hello"`
	Open    json.RawMessage `json:"open"`
	Refused *struct {
		Open   json.RawMessage `json:"open"`
		Send   json.RawMessage `json:"send"`
		Follow bool            `json:"follow"`
		Code   string          `json:"code"`
		Names  string          `json:"names"`
	} `json:"refused"`
}

func conformanceFiles(t *testing.T) (scenarios []conformanceScenario, base map[string]json.RawMessage) {
	t.Helper()
	root := "../../sidecar/conformance"
	data, err := os.ReadFile(filepath.Join(root, "open_request.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &base); err != nil {
		t.Fatal(err)
	}
	files, err := filepath.Glob(filepath.Join(root, "scenarios", "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		var scenario conformanceScenario
		if err := json.Unmarshal(data, &scenario); err != nil {
			t.Fatalf("%s: %v", file, err)
		}
		before := slices.ContainsFunc(scenario.Steps, func(step conformanceStep) bool {
			return step.Hello != nil || step.Open != nil || step.Refused != nil
		})
		if before {
			scenarios = append(scenarios, scenario)
		}
	}
	if len(scenarios) == 0 {
		t.Fatal("no scenario about the time before a session")
	}
	return scenarios, base
}

func requestOver(t *testing.T, base map[string]json.RawMessage, overrides json.RawMessage) *pb.OpenSessionRequest {
	t.Helper()
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(overrides, &fields); err != nil {
		t.Fatal(err)
	}
	merged := map[string]json.RawMessage{}
	for k, v := range base {
		merged[k] = v
	}
	for k, v := range fields {
		merged[k] = v
	}
	data, err := json.Marshal(merged)
	if err != nil {
		t.Fatal(err)
	}
	var request pb.OpenSessionRequest
	if err := protojson.Unmarshal(data, &request); err != nil {
		t.Fatal(err)
	}
	return &request
}

func TestTheHolderATestRunsAgainstPlaysTheConformanceCorpus(t *testing.T) {
	scenarios, base := conformanceFiles(t)
	for _, scenario := range scenarios {
		t.Run(scenario.Name, func(t *testing.T) {
			w := newHolderWorld()
			place := HolderPlace{Socket: "/s/sock", Directory: "/s"}
			if _, err := w.Spawn(place, []string{"/bin/holder"}, place.Bootstrap()); err != nil {
				t.Fatal(err)
			}
			link, err := w.Dial(context.Background(), place)
			if err != nil {
				t.Fatal(err)
			}
			ctx := context.Background()
			for i, step := range scenario.Steps {
				switch {
				case step.Hello != nil:
					client := sessionVersions()
					if step.Hello.Client != nil {
						if err := protojson.Unmarshal(step.Hello.Client, client); err != nil {
							t.Fatal(err)
						}
					}
					answer, err := link.Hello(ctx, &pb.SessionHelloRequest{Client: client})
					if err != nil {
						t.Fatalf("step %d: %v", i, err)
					}
					if got := answer.GetCompatibility().String(); got != step.Hello.Compatibility {
						t.Fatalf("step %d: compatibility %s, want %s", i, got, step.Hello.Compatibility)
					}
					if want := step.Hello.Session; (want == nil) != (answer.GetSession() == nil) ||
						(want != nil && (want.AgentID != answer.GetSession().GetAgentId() || want.Session != answer.GetSession().GetSession())) {
						t.Fatalf("step %d: session %v, want %v", i, answer.GetSession(), want)
					}
				case step.Open != nil:
					if _, err := link.Open(ctx, requestOver(t, base, step.Open)); err != nil {
						t.Fatalf("step %d: %v", i, err)
					}
				case step.Refused != nil:
					var err error
					switch {
					case step.Refused.Open != nil:
						_, err = link.Open(ctx, requestOver(t, base, step.Refused.Open))
					case step.Refused.Send != nil:
						err = link.Send(ctx, &pb.SendRequest{})
					default:
						_, err = link.Follow(ctx, 0, func(e error) error { return e })
					}
					got := status.Convert(err)
					if got.Code().String() != codeName(step.Refused.Code) {
						t.Fatalf("step %d: %v, want %s", i, err, step.Refused.Code)
					}
					if !strings.Contains(got.Message(), step.Refused.Names) {
						t.Fatalf("step %d: %q does not name %s", i, got.Message(), step.Refused.Names)
					}
				}
			}
		})
	}
}

// codeName is the gRPC Go name of a code the corpus spells as Python and the wire do.
func codeName(code string) string {
	parts := strings.Split(strings.ToLower(code), "_")
	for i, part := range parts {
		parts[i] = strings.ToUpper(part[:1]) + part[1:]
	}
	return strings.Join(parts, "")
}
