package clientcli_test

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

const (
	plannerID = "agent-2ae74088c0de"
	workerID  = "agent-9f01b7d4e5a2"
)

func checkGolden(t *testing.T, name, got string) {
	t.Helper()
	golden := filepath.Join("testdata", name)
	if os.Getenv("CLIENTCLI_UPDATE_GOLDEN") != "" {
		if err := os.WriteFile(golden, []byte(got), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(golden)
	if err != nil {
		t.Fatal(err)
	}
	if got != string(want) {
		t.Errorf("output differs from %s (CLIENTCLI_UPDATE_GOLDEN=1 rewrites it)\n--- got\n%s--- want\n%s", golden, got, want)
	}
}

// With no terminal the lead's view is plain lines: its conversation in full, tool calls abridged,
// turns with what started them and what they cost, and the other agents as short lines.
func TestAttachWithNoTerminalPrintsTheLeadsConversation(t *testing.T) {
	w := newWorld(t, serveFake(t, conversation(pb.RunStatus_RUN_STATUS_ENDED)))
	got := w.run(context.Background(), "run", "attach", "run-1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	checkGolden(t, "attach_lead.golden", got.stdout)
	if strings.Contains(got.stdout, "\x1b") {
		t.Errorf("a plain view holds an escape: %q", got.stdout)
	}
	if strings.Contains(got.stdout, "2.40M") {
		t.Errorf("a turn's input-token figure was shown:\n%s", got.stdout)
	}
	if strings.Contains(got.stdout, "agent-") {
		t.Errorf("an agent id was shown in the lead's view:\n%s", got.stdout)
	}
}

// --agent starts in that agent's conversation, by the name the view shows or by id, and with no
// terminal follows only it.
func TestAttachAgentFollowsOnlyThatAgent(t *testing.T) {
	var first string
	for _, name := range []string{"worker-1", workerID, "agent-9f01"} {
		w := newWorld(t, serveFake(t, conversation(pb.RunStatus_RUN_STATUS_ENDED)))
		got := w.run(context.Background(), "run", "attach", "run-1", "--agent", name)
		if got.code != 0 {
			t.Fatalf("--agent %s: exit %d, stderr %q", name, got.code, got.stderr)
		}
		if first == "" {
			first = got.stdout
			checkGolden(t, "attach_worker.golden", got.stdout)
			for _, other := range []string{"I will look at the repo", "Plan 1 caches", "planner-1 started"} {
				if strings.Contains(got.stdout, other) {
					t.Errorf("worker's view holds %q:\n%s", other, got.stdout)
				}
			}
		} else if got.stdout != first {
			t.Errorf("--agent %s differs from --agent worker-1:\n%s\n%s", name, got.stdout, first)
		}
	}
}

func TestAttachAgentNamesTheAgentsWhenItFindsNone(t *testing.T) {
	w := newWorld(t, serveFake(t, conversation(pb.RunStatus_RUN_STATUS_ENDED)))
	got := w.run(context.Background(), "run", "attach", "run-1", "--agent", "judge-1")
	if got.code != 1 {
		t.Fatalf("exit %d, want 1", got.code)
	}
	contains(t, "stderr", got.stderr, `no agent "judge-1"`, "lead, planner-1, worker-1")
}

// A long run is not replayed in full: the newest 200 items, a line saying how many came before, and
// a flag for all of them. The service gives 75 at a time, so this reads several pages.
func TestAttachReplaysTheNewestItemsAndSaysHowManyItSkipped(t *testing.T) {
	const total = 230
	var items []*pb.Item
	for i := 1; i <= total; i++ {
		items = append(items, assistantItem("lead-run-1", uint32(i), "t1", fmt.Sprintf("message %d", i)))
	}
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	run.EndedAt = at(20)
	turn := &pb.Turn{Id: "t1", AgentId: "lead-run-1", Ordinal: 1, Trigger: pb.TurnTrigger_TURN_TRIGGER_BRIEF, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(0), EndedAt: at(1)}
	frames := []*pb.WatchRunsResponse{{Body: &pb.WatchRunsResponse_Snapshot{Snapshot: &pb.RunsSnapshot{
		Runs: []*pb.Run{run}, Agents: []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ENDED)}, Turns: []*pb.Turn{turn}, Sequence: 3}}}}

	w := newWorld(t, serveFake(t, &fakeService{frames: frames, items: map[string][]*pb.Item{"lead-run-1": items}}))
	got := w.run(context.Background(), "run", "attach", "run-1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "(30 earlier items of lead are not shown; --full-history shows them all)", "* message 31\n", "* message 230\n")
	if strings.Contains(got.stdout, "* message 30\n") || strings.Count(got.stdout, "* message ") != 200 {
		t.Errorf("replayed %d messages, including the 30th; want the newest 200", strings.Count(got.stdout, "* message "))
	}

	w = newWorld(t, serveFake(t, &fakeService{frames: frames, items: map[string][]*pb.Item{"lead-run-1": items}}))
	all := w.run(context.Background(), "run", "attach", "run-1", "--full-history")
	if strings.Count(all.stdout, "* message ") != total || strings.Contains(all.stdout, "earlier items") {
		t.Errorf("--full-history replayed %d messages:\n%.300s", strings.Count(all.stdout, "* message "), all.stdout)
	}
	for i := 1; i <= total; i++ {
		if !strings.Contains(all.stdout, fmt.Sprintf("* message %d\n", i)) {
			t.Fatalf("--full-history lacks message %d", i)
		}
	}
}

// --- a terminal ---

// fakeScreen is a terminal that remembers how it was used.
type fakeScreen struct {
	mu              sync.Mutex
	entered         int
	restored        int
	resize          chan struct{}
	panicOnResizes  bool
	width, height   int
	refusesRawInput bool
}

func newScreen() *fakeScreen { return &fakeScreen{resize: make(chan struct{}), width: 80, height: 24} }

func (s *fakeScreen) Raw() (func(), error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.refusesRawInput {
		return nil, fmt.Errorf("not a terminal")
	}
	s.entered++
	return func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		s.restored++
	}, nil
}

func (s *fakeScreen) Size() (int, int) { return s.width, s.height }

func (s *fakeScreen) Resizes() <-chan struct{} {
	if s.panicOnResizes {
		panic("the resize reader broke")
	}
	return s.resize
}

func (s *fakeScreen) counts() (entered, restored int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.entered, s.restored
}

// terminalWorld is a world at a terminal whose keys the test writes to the returned writer.
func terminalWorld(t *testing.T, f *fakeService) (*world, *fakeScreen, *io.PipeWriter) {
	t.Helper()
	w := newWorld(t, serveFake(t, f))
	pr, pw := io.Pipe()
	t.Cleanup(func() { pw.Close() })
	screen := newScreen()
	w.terminal, w.screen, w.stdin = true, screen, pr
	return w, screen, pw
}

func key(t *testing.T, pw *io.PipeWriter, keys string) {
	t.Helper()
	if _, err := pw.Write([]byte(keys)); err != nil {
		t.Errorf("writing key %q: %v", keys, err)
	}
}

// waitEnded waits, with a bound that only a hang would reach, until the service has seen the streams of
// these agents end.
func waitEnded(t *testing.T, f *fakeService, agents ...string) {
	t.Helper()
	pending := map[string]bool{}
	for _, a := range agents {
		pending[a] = true
	}
	deadline := time.After(guard)
	for len(pending) > 0 {
		select {
		case a := <-f.ended:
			delete(pending, a)
		case <-deadline:
			t.Errorf("the streams of %v never ended", pending)
			return
		}
	}
}

// At a terminal, a lists the agents and a number opens one's conversation on a cleared screen, Esc goes up
// to the agent that started it and on to the lead, the old stream ends at each switch, and Ctrl-C detaches
// with the terminal put back.
func TestAttachAtATerminalSwitchesViewsAndPutsTheTerminalBack(t *testing.T) {
	// The worker was started by the planner, so Esc from the worker goes to the planner and then the lead.
	f := conversation(pb.RunStatus_RUN_STATUS_RUNNING, func(_ *pb.Run, agents []*pb.Agent) { agents[2].ParentAgentId = plannerID })
	w, screen, pw := terminalWorld(t, f)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	step := 0
	w.onStdout = func(written string) {
		switch {
		case step == 0 && strings.Contains(written, "ask a planner for a plan"):
			step = 1
			key(t, pw, "a")
		case step == 1 && strings.Contains(written, "worker-1") && strings.Contains(written, "type a number"):
			step = 2
			key(t, pw, "3")
		case step == 2 && strings.Contains(written, "Done: 9% faster"):
			step = 3
			key(t, pw, "\x1b")
		case step == 3 && strings.Contains(written, "Plan 1 caches"):
			step = 4
			key(t, pw, "b")
		case step == 4 && strings.Count(written, "ask a planner for a plan") == 2:
			step = 5
			waitEnded(t, f, workerID, plannerID)
			cancel()
		}
	}
	got := w.runUnbounded(ctx, "run", "attach", "run-1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	if step != 5 {
		t.Fatalf("the script stopped at step %d:\n%q", step, got.stdout)
	}
	contains(t, "stdout", got.stdout, "agents (type a number", "-- worker-1: worker", "-- planner-1: planner", "-- lead:", "viewing worker-1", "viewing planner-1", "viewing lead", "Esc back", "Ctrl-C detaches")
	if n := strings.Count(got.stdout, "\x1b[2J"); n != 3 {
		t.Errorf("the screen was cleared %d times, want 3 (a switch each)", n)
	}
	if strings.Count(got.stdout, "ask a planner for a plan") != 2 {
		t.Errorf("the lead's conversation was replayed %d times, want 2", strings.Count(got.stdout, "ask a planner for a plan"))
	}
	// the numbers and what they mean
	contains(t, "stdout", got.stdout, "   1  lead", "   2  planner-1", "   3  worker-1")
	if entered, restored := screen.counts(); entered != 1 || restored != 1 {
		t.Errorf("the terminal was put in key mode %d times and back %d times", entered, restored)
	}
	if !strings.HasSuffix(got.stdout, "\r\x1b[2K") {
		t.Errorf("the footer was left on the screen: %q", got.stdout[max(0, len(got.stdout)-60):])
	}
	contains(t, "stderr", got.stderr, "detached: run run-1 is still going")
	f.mu.Lock()
	opened := append([]string(nil), f.opened...)
	f.mu.Unlock()
	if want := []string{"lead-run-1", workerID, plannerID, "lead-run-1"}; fmt.Sprint(opened) != fmt.Sprint(want) {
		t.Errorf("streams opened %v, want %v", opened, want)
	}
	if sent := f.sent(); len(sent) != 0 {
		t.Errorf("a detach sent %v", sent)
	}
}

// Whatever ends the view, the terminal is put back: the run ending, a broken stream, and a reader that
// panics.
func TestAttachAtATerminalPutsTheTerminalBackWhateverEndsTheView(t *testing.T) {
	cases := []struct {
		name  string
		run   pb.RunStatus
		setup func(*fakeService, *fakeScreen)
		code  int
	}{
		{"the run ends", pb.RunStatus_RUN_STATUS_ENDED, func(*fakeService, *fakeScreen) {}, 0},
		{"the run fails", pb.RunStatus_RUN_STATUS_FAILED, func(*fakeService, *fakeScreen) {}, 1},
		{"a stream breaks", pb.RunStatus_RUN_STATUS_RUNNING, func(f *fakeService, _ *fakeScreen) {
			f.agentErr = status.Error(codes.Unavailable, "the host went away")
		}, 4},
		{"a reader panics", pb.RunStatus_RUN_STATUS_RUNNING, func(_ *fakeService, s *fakeScreen) { s.panicOnResizes = true }, 1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := conversation(c.run)
			w, screen, _ := terminalWorld(t, f)
			c.setup(f, screen)
			got := w.run(context.Background(), "run", "attach", "run-1")
			if got.code != c.code {
				t.Errorf("exit %d, want %d; stderr %q", got.code, c.code, got.stderr)
			}
			if entered, restored := screen.counts(); entered != 1 || restored != 1 {
				t.Errorf("the terminal was put in key mode %d times and back %d times", entered, restored)
			}
			if strings.Contains(got.stdout, "\x1b[2K") && !strings.HasSuffix(got.stdout, "\x1b[2K") && !strings.HasSuffix(got.stdout, "\n") {
				t.Errorf("a footer was left on the last line: %q", got.stdout[max(0, len(got.stdout)-60):])
			}
		})
	}
}

// A terminal that will not give single keys gets the plain view, and is told.
func TestAttachAtATerminalThatRefusesKeysIsPlain(t *testing.T) {
	f := conversation(pb.RunStatus_RUN_STATUS_ENDED)
	w, screen, _ := terminalWorld(t, f)
	screen.refusesRawInput = true
	got := w.run(context.Background(), "run", "attach", "run-1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stderr", got.stderr, "the view is plain lines")
	if strings.Contains(got.stdout, "\x1b") || strings.Contains(got.stdout, "viewing") {
		t.Errorf("a terminal that gave no keys got controls: %q", got.stdout)
	}
}

// What attach shows by an agent's name works as --agent of steer, which sends the full id.
func TestSteerAcceptsTheNameAttachShows(t *testing.T) {
	f := conversation(pb.RunStatus_RUN_STATUS_RUNNING)
	w := newWorld(t, serveFake(t, f))
	for _, name := range []string{"worker-1", "Worker-1", workerID} {
		got := w.run(context.Background(), "run", "steer", "run-1", "hello", "--agent", name, "--json")
		if got.code != 0 {
			t.Fatalf("--agent %s: exit %d, stderr %q", name, got.code, got.stderr)
		}
	}
	for _, c := range f.sent() {
		if id := c.GetSendMessage().GetAgentId(); id != workerID {
			t.Errorf("steer sent to %q, want the id %q", id, workerID)
		}
	}
	if len(f.sent()) != 3 {
		t.Errorf("%d steers were sent, want 3", len(f.sent()))
	}
}
