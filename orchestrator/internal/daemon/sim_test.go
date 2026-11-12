package daemon_test

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/prompts"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// The simulation runs a whole daemon (engine, effect worker, sidecar streams, job poll, service and
// hub) over Fakes in one thread, from one seed. Around it go the faults a deployment has: the
// daemon is killed and restarted, sidecars die, clients retry, stop reading and reconnect, and the
// job source fails. The world is also unkind to the daemon's own calls: a sidecar call hangs until its
// bound ends, the store fails to settle a row, and the process ends inside a step (after the claim,
// after a command was sent, before the rows are completed). After every step the state must satisfy the core's invariants, and at the end
// what the store recovers must equal the live state, and every watcher's view must equal the
// daemon's.

// counter tallies what the seeds between them reached, so a fault the seeds never injected fails
// the run instead of passing silently.
type counter map[string]int

type sim struct {
	t      *testing.T
	seed   uint64
	rng    *rand.Rand
	rig    *daemontest.Rig
	steps  []string
	issued []*pb.Command
	stuck  map[string]*daemontest.Session // turn id -> its session, while the fake holds the turn.
	runs   []*simWatcher
	agents map[core.AgentID]*itemWatcher
	noops  int
	next   int // the next command id, never reused.
	tally  counter
	// usage draws what the turns the fake sidecars end report, apart from the rng that steers the
	// simulation, so that giving turns a cost leaves every seed's course as it was.
	usage *rand.Rand
	// clockMoved says the clock advanced since the daemon last settled, so a timer may be due.
	clockMoved bool
}

func (s *sim) note(format string, args ...any) {
	s.steps = append(s.steps, fmt.Sprintf(format, args...))
}

func (s *sim) fail(format string, args ...any) {
	s.t.Helper()
	tail := s.steps
	if len(tail) > 25 {
		tail = tail[len(tail)-25:]
	}
	s.t.Fatalf("seed %d: %s\nreplay: ORCHESTRATOR_SIM_SEED=%d go test ./internal/daemon -run 'TestDaemonSimulation$'\nlast steps:\n  %s",
		s.seed, fmt.Sprintf(format, args...), s.seed, strings.Join(tail, "\n  "))
}

// noopLog counts the inputs that changed nothing.
type noopLog struct{ n *int }

func (noopLog) Enabled(context.Context, slog.Level) bool { return true }
func (h noopLog) Handle(_ context.Context, r slog.Record) error {
	if r.Message == "input changed nothing" {
		*h.n++
	}
	return nil
}
func (h noopLog) WithAttrs([]slog.Attr) slog.Handler { return h }
func (h noopLog) WithGroup(string) slog.Handler      { return h }

// view is a client's picture of the runs, folded from a snapshot and the events after it.
type view struct {
	runs   map[string]*pb.Run
	agents map[string]*pb.Agent
	seq    uint64
	// messages and turns are what the last snapshot held. Changes after it are not folded into them.
	messages []*pb.Message
	turns    []*pb.Turn
	// foldedMessages and foldedTurns start from the last snapshot, with the changes after it folded in.
	foldedMessages map[string]*pb.Message
	foldedTurns    map[string]*pb.Turn
	// truncated says the last snapshot left history out, so a fold cannot hold it all.
	truncated bool
	// foldedComplete says the fold began at a snapshot, so it holds the whole history.
	foldedComplete bool
}

func (v *view) snapshot(snap *pb.RunsSnapshot) {
	v.runs, v.agents, v.seq = map[string]*pb.Run{}, map[string]*pb.Agent{}, snap.GetSequence()
	v.messages, v.turns = snap.GetMessages(), snap.GetTurns()
	v.foldedMessages, v.foldedTurns, v.truncated, v.foldedComplete = map[string]*pb.Message{}, map[string]*pb.Turn{}, snap.GetTruncated(), true
	for _, m := range snap.GetMessages() {
		v.foldedMessages[m.GetId()] = m
	}
	for _, t := range snap.GetTurns() {
		v.foldedTurns[t.GetId()] = t
	}
	for _, r := range snap.GetRuns() {
		v.runs[r.GetId()] = r
	}
	for _, a := range snap.GetAgents() {
		v.agents[a.GetId()] = a
	}
}

// simWatcher is a WatchRuns client the simulation reads at times it chooses.
type simWatcher struct {
	w *daemon.RunsWatch
	v view
}

// openRuns opens a watch after a sequence. A client that resumes keeps the view it had; one that
// starts over (after 0) holds nothing and takes the snapshot.
func (s *sim) openRuns(after uint64, held view) *simWatcher {
	w, first, err := s.rig.Service.WatchRunsFrom(core.Operator{}, after)
	if err != nil {
		s.fail("open a runs watch after %d: %v", after, err)
	}
	sw := &simWatcher{w: w, v: view{
		runs: map[string]*pb.Run{}, agents: map[string]*pb.Agent{}, seq: after,
		foldedMessages: map[string]*pb.Message{}, foldedTurns: map[string]*pb.Turn{},
	}}
	if after > 0 {
		sw.v = held
	}
	s.feed(sw, first)
	return sw
}

func (s *sim) feed(sw *simWatcher, msgs []*pb.WatchRunsResponse) {
	for _, m := range msgs {
		snap, ev, search := client.DecodeWatchRuns(m)
		if snap != nil {
			sw.v.snapshot(snap)
			s.tally["snapshot"]++
			continue
		}
		if search != nil {
			if err := wire.Validate(search); err != nil {
				s.fail("invalid search state: %v", err)
			}
			continue
		}
		if ev.GetSequence() != sw.v.seq+1 {
			s.fail("a watcher at %d was sent event %d: an event was lost, repeated or reordered", sw.v.seq, ev.GetSequence())
		}
		sw.v.seq = ev.GetSequence()
		switch b := ev.GetBody().(type) {
		case *pb.RunChange_Run:
			sw.v.runs[b.Run.GetId()] = b.Run
		case *pb.RunChange_Agent:
			sw.v.agents[b.Agent.GetId()] = b.Agent
		case *pb.RunChange_Turn:
			sw.v.foldedTurns[b.Turn.GetId()] = b.Turn
		case *pb.RunChange_Message:
			sw.v.foldedMessages[b.Message.GetId()] = b.Message
		}
	}
}

func (s *sim) poll(sw *simWatcher) {
	msgs, err := sw.w.Poll()
	if err != nil {
		s.fail("poll: %v", err)
	}
	s.feed(sw, msgs)
}

// itemWatcher is a WatchAgent client.
type itemWatcher struct {
	w     *daemon.AgentWatch
	items map[string]*pb.Item
}

func (s *sim) feedItems(iw *itemWatcher, msgs []*pb.WatchAgentResponse) {
	for _, m := range msgs {
		switch b := m.GetBody().(type) {
		case *pb.WatchAgentResponse_Page:
			iw.items = map[string]*pb.Item{}
			for _, it := range b.Page.GetItems() {
				iw.items[it.GetId()] = it
			}
		case *pb.WatchAgentResponse_Item:
			iw.items[b.Item.GetId()] = b.Item
		case *pb.WatchAgentResponse_Delta:
			s.tally["delta"]++
		}
	}
}

func (s *sim) operator(cmd *pb.Command) (*pb.Receipt, error) {
	resp, err := s.rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: cmd})
	if err != nil {
		return nil, err
	}
	return resp.GetReceipt(), nil
}

func (s *sim) agentCtx(a core.Agent, token string, session uint32) context.Context {
	return metadata.NewIncomingContext(context.Background(), metadata.Pairs(
		daemon.MetaAgent, string(a.ID), daemon.MetaSession, fmt.Sprint(session), daemon.MetaToken, token))
}

// tolerated are the errors a step reports for the faults the simulation injects.
func tolerated(err error) bool {
	return strings.Contains(err.Error(), "source unavailable") || errors.Is(err, daemon.ErrExpired) || errors.Is(err, daemontest.ErrInjected)
}

// stepDaemon runs one step. A crash the store was told to inject ends the daemon there: the world
// keeps what the step had done, and a new daemon starts over what was committed.
func (s *sim) stepDaemon() (moved bool, err error) {
	defer func() {
		if r := recover(); r != nil {
			crash, ok := r.(daemontest.Crash)
			if !ok {
				panic(r)
			}
			s.note("the daemon crashes %s", crash.At)
			s.tally["crash "+crash.At]++
			s.restart()
			moved, err = true, nil
		}
	}()
	return s.rig.Daemon.Step(context.Background())
}

func (s *sim) settle() {
	for range 5000 {
		s.rig.Hub.Flush()
		moved, err := s.stepDaemon()
		if err != nil && !tolerated(err) {
			s.fail("step: %v", err)
		}
		if !moved {
			s.clockMoved = false
			return
		}
	}
	s.fail("the daemon never settled")
}

// clearFaults spends every fault still pending, so that the daemon can settle.
func (s *sim) clearFaults() {
	h := s.rig.Sidecars
	h.Stall, h.AfterSend = 0, nil
	s.rig.JobStall = 0
	s.rig.Faults.FailComplete, s.rig.Faults.FailRelease, s.rig.Faults.CrashAt = 0, 0, ""
}

// injectFault arms one fault for the next steps of the daemon.
func (s *sim) injectFault() {
	switch s.pick(6) {
	case 0:
		s.rig.Sidecars.Stall = 1 + s.pick(3)
		s.note("the next %d sidecar calls hang", s.rig.Sidecars.Stall)
		s.tally["stalled call"]++
	case 1:
		s.rig.Faults.FailComplete = 1 + s.pick(2)
		s.note("the store fails to complete %d rows", s.rig.Faults.FailComplete)
	case 2:
		s.rig.Faults.FailRelease = 1 + s.pick(2)
		s.note("the store fails to release %d rows", s.rig.Faults.FailRelease)
	case 3:
		s.rig.Faults.CrashAt = daemontest.CrashAfterClaim
		s.note("the daemon will crash after its next claim")
	case 4:
		s.rig.Faults.CrashAt = daemontest.CrashBeforeComplete
		s.note("the daemon will crash before its next complete")
	case 5:
		armed := true
		s.rig.Sidecars.AfterSend = func() {
			if armed {
				armed = false
				panic(daemontest.Crash{At: "after send"})
			}
		}
		s.note("the daemon will crash after its next command is sent")
	}
}

// checkDelivered holds the daemon to its second promise: every effect committed for a session that
// is still live reaches that session's sidecar. It runs after the faults are spent and the daemon
// has settled, when nothing but a bug can keep one back.
func (s *sim) checkDelivered() {
	s.clearFaults()
	s.settle()
	state := s.rig.Daemon.State()
	for _, e := range s.rig.Faults.Committed {
		cmd, ok, err := wire.SessionCommandFor(e, prompts.Render)
		if err != nil || !ok {
			continue
		}
		var session core.Session
		switch b := e.(type) {
		case core.StartTurn:
			session = b.Session
		case core.Steer:
			session = b.Session
		case core.Interrupt:
			session = b.Session
		case core.CloseSession:
			session = b.Session
		default:
			continue
		}
		if a, live := state.Agent(session.Agent); !live || a.Session != session.N || a.Status.Over() {
			continue
		}
		delivered := false
		if sidecar := s.rig.Sidecars.Session(session); sidecar != nil {
			for _, got := range sidecar.Commands() {
				delivered = delivered || got.GetEffectId() == cmd.GetEffectId()
			}
		}
		if !delivered {
			s.fail("effect %s was committed for %s/%d, which is still live, and never reached its sidecar", e.ID(), session.Agent, session.N)
		}
	}
	s.checkJobCount()
	s.tally["delivery checked"]++
}

// checkJobCount holds each live run's count of running jobs to what the job source reports. The
// daemon polls on a timer, and a poll that failed leaves the last count, so the clock moves for two
// polls: the second is the first that nothing fails.
func (s *sim) checkJobCount() {
	for range 2 {
		s.rig.Clock.Advance(daemontest.PollEvery)
		s.settle()
	}
	for _, run := range s.rig.Daemon.State().Runs() {
		if !run.Status.Live() {
			continue
		}
		updates, err := s.rig.Jobs.Jobs(context.Background(), run)
		if err != nil {
			continue
		}
		var going uint32
		for _, u := range updates {
			if !u.State.Final() {
				going++
			}
		}
		if run.ActiveJobs != going {
			s.fail("run %s counts %d jobs going, and the job source reports %d", run.ID, run.ActiveJobs, going)
		}
		s.tally["job count checked"]++
	}
}

func (s *sim) liveAgents() []core.Agent {
	var out []core.Agent
	st := s.rig.Daemon.State()
	for _, r := range st.Runs() {
		for _, a := range st.Agents(r.ID) {
			if !a.Status.Over() {
				out = append(out, a)
			}
		}
	}
	return out
}

func (s *sim) id(prefix string) string {
	s.next++
	return fmt.Sprintf("%s-%d", prefix, s.next)
}

func (s *sim) pick(n int) int { return s.rng.IntN(n) }

func (s *sim) step() {
	st := s.rig.Daemon.State()
	runs := st.Runs()
	live := s.liveAgents()
	switch roll := s.pick(100); {
	case roll < 8 && len(runs) < 3:
		cmd := &pb.Command{Id: s.id("op"), Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
			Repository: "/repo", Task: "task", FanoutLimit: uint32(1 + s.pick(4)), Brief: "go",
			Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
		}}}
		if s.pick(3) == 0 {
			cmd.GetStartRun().MaxTime = durationpb.New(time.Duration(1+s.pick(600)) * time.Second)
		}
		s.note("operator starts a run (%s)", cmd.Id)
		s.record(cmd)
	case roll < 14 && len(runs) > 0:
		cmd := &pb.Command{Id: s.id("op"), Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(runs[s.pick(len(runs))].ID)}}}
		s.note("operator stops %s", cmd.GetStopRun().GetRunId())
		s.record(cmd)
	case roll < 22 && len(live) > 0:
		a := live[s.pick(len(live))]
		cmd := &pb.Command{Id: s.id("op"), Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(a.ID), Text: "steer"}}}
		s.note("operator tells %s", a.ID)
		s.record(cmd)
	case roll < 34 && len(live) > 0:
		s.agentAct(live[s.pick(len(live))])
	case roll < 40 && len(s.issued) > 0:
		s.retry()
	case roll < 43 && len(s.issued) > 0:
		s.conflict()
	case roll < 50:
		for _, d := range []time.Duration{0, time.Second, daemontest.PollEvery, core.StopGrace + time.Second, time.Hour} {
			if s.pick(5) == 0 {
				s.note("clock advances %s", d)
				s.rig.Clock.Advance(d)
				s.clockMoved = s.clockMoved || d > 0
				break
			}
		}
	case roll < 56 && len(runs) > 0:
		run := runs[s.pick(len(runs))]
		job := core.JobUpdate{Job: core.JobID(fmt.Sprintf("job-%d", s.pick(4))), Run: run.ID, State: []core.JobState{core.JobRunning, core.JobSucceeded, core.JobFailed, core.JobLost}[s.pick(4)]}
		if len(live) > 0 {
			job.SubmittedBy = live[s.pick(len(live))].ID
		}
		s.note("job %s of %s is %s", job.Job, run.ID, job.State)
		s.rig.Jobs.Set(job)
		if s.pick(4) == 0 {
			s.rig.Jobs.FailNext()
		}
	case roll < 64 && len(s.stuck) > 0:
		s.finishTurn()
	case roll < 68 && len(live) > 0:
		a := live[s.pick(len(live))]
		if se := s.rig.Sidecars.Session(core.Session{Agent: a.ID, N: a.Session}); se != nil && !se.Dead() {
			s.note("sidecar of %s/%d dies", a.ID, a.Session)
			se.Die()
			s.tally["sidecar death"]++
		}
	case roll < 72:
		s.note("daemon killed and restarted")
		s.restart()
	case roll < 76:
		s.rig.Sidecars.LaunchFailures = 1 + s.pick(2)
		s.note("the next %d launches fail", s.rig.Sidecars.LaunchFailures)
	case roll < 82:
		s.openWatchers()
	case roll < 86:
		s.pollSome()
	case roll < 92:
		s.injectFault()
	case roll < 96:
		s.note("settle and check delivery")
		s.checkDelivered()
	case roll < 98:
		// A quiet longer than a lead may be left alone: runs that are stalled remind their leads.
		s.note("everything settles and a long quiet follows")
		s.settle()
		s.rig.Clock.Advance(core.LeadIdleAfter + time.Second)
		s.settle()
	default:
		s.note("settle")
		s.settle()
	}
}

func (s *sim) record(cmd *pb.Command) {
	receipt, err := s.operator(cmd)
	if err != nil {
		s.fail("dispatch %s: %v", cmd.GetId(), err)
	}
	s.issued = append(s.issued, cmd)
	if receipt.GetCommandId() != cmd.GetId() {
		s.fail("receipt %v is for another command than %s", receipt, cmd.GetId())
	}
}

// retry sends an old command again. The answer is the first one, whatever happened since.
func (s *sim) retry() {
	cmd := s.issued[s.pick(len(s.issued))]
	first, _ := s.operator(cmd)
	again, err := s.operator(cmd)
	if err != nil {
		s.fail("retry of %s: %v", cmd.GetId(), err)
	}
	a, b := proto.Clone(first).(*pb.Receipt), proto.Clone(again).(*pb.Receipt)
	a.Sequence, b.Sequence = 0, 0
	if !proto.Equal(a, b) {
		s.fail("retry of %s answered %v, then %v", cmd.GetId(), a, b)
	}
	s.note("retry of %s", cmd.GetId())
	s.tally["retry"]++
}

// conflict reuses an id for a different command, which must be refused and change nothing.
func (s *sim) conflict() {
	old := s.issued[s.pick(len(s.issued))]
	other := &pb.Command{Id: old.GetId(), Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: "run-elsewhere-" + fmt.Sprint(s.pick(1000))}}}
	if proto.Equal(old, other) {
		return
	}
	before := s.rig.Daemon.State()
	_, err := s.operator(other)
	if status.Code(err) != codes.AlreadyExists {
		s.fail("reusing id %s for another command gave %v, want AlreadyExists", old.GetId(), err)
	}
	if !before.Equal(s.rig.Daemon.State()) {
		s.fail("a conflicting command changed the state")
	}
	s.note("conflict on %s refused", old.GetId())
	s.tally["conflict"]++
}

// agentAct makes an agent call its tools through the service, with and without its credentials.
func (s *sim) agentAct(a core.Agent) {
	se := s.rig.Sidecars.Session(core.Session{Agent: a.ID, N: a.Session})
	if se == nil {
		return // its launch has not been carried out.
	}
	token := se.Credentials.GetToken()
	id := s.id("ag-" + string(a.ID))
	var cmd *pb.Command
	switch {
	case a.Role == core.RoleLead && s.pick(2) == 0:
		cmd = &pb.Command{Id: id, Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{
			RunId: string(a.Run), Role: pb.AgentRole(2 + s.pick(5)), Brief: "work", PlanNumber: uint32(s.pick(3)),
		}}}
	case a.Role == core.RoleLead && s.pick(2) == 0:
		cmd = &pb.Command{Id: id, Body: &pb.Command_PostReport{PostReport: &pb.PostReport{Report: &pb.Report{Kind: pb.ReportKind_REPORT_KIND_PROGRESS, Headline: "h"}}}}
	case a.Role == core.RoleLead:
		live := s.liveAgents()
		cmd = &pb.Command{Id: id, Body: &pb.Command_StopAgent{StopAgent: &pb.StopAgent{AgentId: string(live[s.pick(len(live))].ID)}}}
	default:
		cmd = &pb.Command{Id: id, Body: &pb.Command_PostHandoff{PostHandoff: &pb.PostHandoff{Handoff: handoffBy(a.Role)}}}
	}
	switch s.pick(8) {
	case 0: // a wrong token must change nothing.
		before := s.rig.Daemon.State()
		_, err := s.rig.AgentService.Dispatch(s.agentCtx(a, "00", a.Session), &pb.DispatchRequest{Command: cmd})
		if status.Code(err) != codes.Unauthenticated || !before.Equal(s.rig.Daemon.State()) {
			s.fail("a wrong token gave %v", err)
		}
		s.tally["bad token refused"]++
		s.note("%s tries a wrong token", a.ID)
		return
	case 1: // a session that is not the current one is refused even with a valid token for it.
		stale := a.Session + 1
		before := s.rig.Daemon.State()
		_, err := s.rig.AgentService.Dispatch(s.agentCtx(a, s.rig.Auth.Token(core.Session{Agent: a.ID, N: stale}), stale), &pb.DispatchRequest{Command: cmd})
		if status.Code(err) != codes.PermissionDenied || !before.Equal(s.rig.Daemon.State()) {
			s.fail("a session that is not current gave %v", err)
		}
		s.tally["stale session refused"]++
		return
	}
	s.note("%s (%s) calls %T", a.ID, a.Role, cmd.GetBody())
	if _, err := s.rig.AgentService.Dispatch(s.agentCtx(a, token, a.Session), &pb.DispatchRequest{Command: cmd}); err != nil {
		s.fail("agent call: %v", err)
	}
}

// finishTurn ends a turn the fake sidecar was holding.
func (s *sim) finishTurn() {
	for turn, se := range s.stuck { // map order is random; the first entry only matters for which turn ends.
		_ = turn
		status := []pb.TurnStatus{pb.TurnStatus_TURN_STATUS_COMPLETED, pb.TurnStatus_TURN_STATUS_FAILED}[s.pick(2)]
		se.EmitEvents(&pb.SessionEvent{Body: &pb.SessionEvent_TurnEnded{TurnEnded: &pb.TurnEnded{TurnId: turn, Status: status}}})
		delete(s.stuck, turn)
		s.note("held turn %s ends %s", turn, status)
		return
	}
}

// liveRuns lists the runs that have not ended, and the agents that were written off.
func (s *sim) liveRuns() (live map[core.RunID]bool, lost map[core.AgentID]bool) {
	live, lost = map[core.RunID]bool{}, map[core.AgentID]bool{}
	st := s.rig.Daemon.State()
	for _, r := range st.Runs() {
		live[r.ID] = r.Status.Live()
		for _, a := range st.Agents(r.ID) {
			lost[a.ID] = a.Status == core.AgentLost || a.Status == core.AgentFailed
		}
	}
	return live, lost
}

// restartIsInnocent says nothing else the simulation did could end a run at this restart: no
// sidecar has died, no launch is set to fail, and no timer is due.
func (s *sim) restartIsInnocent() bool {
	if s.rig.Sidecars.LaunchFailures > 0 || s.clockMoved {
		return false
	}
	for _, a := range s.liveAgents() {
		if se := s.rig.Sidecars.Session(core.Session{Agent: a.ID, N: a.Session}); se != nil && se.Dead() {
			return false
		}
	}
	return true
}

func (s *sim) restart() {
	held := make([]view, len(s.runs))
	for i, w := range s.runs {
		held[i] = w.v
		w.w.Close()
	}
	innocent := s.restartIsInnocent()
	liveBefore, lostBefore := s.liveRuns()
	s.rig.Restart(s.t)
	s.tally["restart"]++
	if innocent {
		// A restart alone never ends a live run or writes off a live agent.
		liveAfter, lostAfter := s.liveRuns()
		for id, was := range liveBefore {
			if was && !liveAfter[id] {
				s.fail("a restart alone ended run %s", id)
			}
		}
		for id, now := range lostAfter {
			if now && !lostBefore[id] {
				s.fail("a restart alone wrote off agent %s", id)
			}
		}
		s.tally["restart alone checked"]++
	}
	s.runs, s.agents = s.runs[:0], map[core.AgentID]*itemWatcher{}
	// Clients reconnect, some resuming after the last sequence they read, some starting over.
	for _, v := range held {
		after := v.seq
		if s.pick(3) == 0 {
			after = 0
		}
		if after > s.rig.Daemon.Last() {
			after = 0
		}
		s.runs = append(s.runs, s.openRuns(after, v))
		s.tally["reconnect"]++
	}
}

func (s *sim) openWatchers() {
	if len(s.runs) < 3 {
		s.runs = append(s.runs, s.openRuns(0, view{}))
		s.note("a client opens a runs watch")
		return
	}
	if live := s.liveAgents(); len(live) > 0 && len(s.agents) < 3 {
		a := live[s.pick(len(live))]
		if s.agents[a.ID] != nil {
			return
		}
		w, first, err := s.rig.Service.WatchAgentFrom(a.ID, 10, 0)
		if err != nil {
			s.fail("watch agent %s: %v", a.ID, err)
		}
		iw := &itemWatcher{w: w, items: map[string]*pb.Item{}}
		s.feedItems(iw, first)
		s.agents[a.ID] = iw
		s.note("a client watches %s", a.ID)
	}
}

// pollSome reads some of the watchers and leaves the rest to fall behind.
func (s *sim) pollSome() {
	for _, w := range s.runs {
		if s.pick(2) == 0 {
			s.poll(w)
		}
	}
	for _, iw := range s.agents {
		if s.pick(2) == 0 {
			msgs, err := iw.w.Poll()
			if err != nil {
				s.fail("poll agent: %v", err)
			}
			s.feedItems(iw, msgs)
		}
	}
}

// init sets up a hold on some turns, and the log that counts inputs that changed nothing.
func (s *sim) init() {
	s.usage = rand.New(rand.NewPCG(s.seed, 31))
	s.rig.Sidecars.React = func(se *daemontest.Session, cmd *pb.SessionCommand) {
		if st := cmd.GetStartTurn(); st != nil && s.pick(4) == 0 {
			s.stuck[st.GetTurnId()] = se
			return
		}
		if st := cmd.GetStartTurn(); st != nil {
			daemontest.AnswerTurnWith(se, st.GetTurnId(), "ok", s.turnUsage())
			return
		}
		daemontest.DefaultReaction(se, cmd)
	}
	s.rig.Log = slog.New(noopLog{n: &s.noops})
	s.rig.Restart(s.t)
}

// turnUsage is what a turn reports: nothing, tokens with no price, or tokens and a price. A price is a
// multiple of 1/1024 so that any order of adding them gives the same sum.
func (s *sim) turnUsage() *pb.Usage {
	switch s.usage.IntN(3) {
	case 0:
		return nil
	case 1:
		n := uint64(s.usage.IntN(900) + 1)
		return &pb.Usage{InputTokens: &n}
	}
	c, n := float64(s.usage.IntN(2048))/1024, uint64(s.usage.IntN(900))
	return &pb.Usage{CostUsd: &c, OutputTokens: &n}
}

// checkCost holds each run's cost view to what the core and the store say, whatever the faults did: the
// view a client is given holds the run's own usage, counts every finished turn the store has and
// every agent the run had, and meets the contract.
func (s *sim) checkCost() {
	state := s.rig.Daemon.State()
	for _, run := range state.Runs() {
		w, first, err := s.rig.Service.WatchRunCostFrom(run.ID)
		if err != nil {
			s.fail("cost of %s: %v", run.ID, err)
		}
		w.Close()
		cost := first[0].GetCost()
		if err := wire.Validate(first[0]); err != nil {
			s.fail("the cost view of %s breaks the contract: %v", run.ID, err)
		}
		if got, want := cost.GetTotal().GetCostUsd(), costOf(run.Usage); !approx(got, want) {
			s.fail("the cost view of %s totals %v, the run's usage is %v", run.ID, got, want)
		}
		if (cost.GetTotal().CostUsd == nil) != (run.Usage.CostUSD == nil) {
			s.fail("the cost view of %s has a total of %v, the run's usage %v", run.ID, cost.GetTotal().CostUsd, run.Usage.CostUSD)
		}
		window, err := s.rig.Store.Turns(run.ID, time.Time{}, time.Date(9999, 1, 1, 0, 0, 0, 0, time.UTC), store.MaxWindowTurns)
		if err != nil {
			s.fail("turns of %s: %v", run.ID, err)
		}
		var finished uint32
		for _, turn := range window.Turns {
			if turn.Status != core.TurnRunning {
				finished++
			}
		}
		var agents uint32
		for _, r := range cost.GetRoles() {
			agents += r.GetAgents()
		}
		if cost.GetTurns() != finished || agents != uint32(len(state.Agents(run.ID))) {
			s.fail("the cost view of %s counts %d turns and %d agents, the store has %d and %d", run.ID, cost.GetTurns(), agents, finished, len(state.Agents(run.ID)))
		}
		s.tally["cost checked"]++
	}
	s.checkHostCost(state)
}

// checkHostCost holds the host's cost view to the runs' own: it meets the contract, counts every run,
// and its total is the sum of the runs' usage, whatever the faults did.
func (s *sim) checkHostCost(state core.State) {
	w, first, err := s.rig.Service.WatchHostCostFrom(time.Time{})
	if err != nil {
		s.fail("host cost: %v", err)
	}
	w.Close()
	host := first[0].GetCost()
	if err := wire.Validate(first[0]); err != nil {
		s.fail("the host cost view breaks the contract: %v", err)
	}
	var usage float64
	var priced bool
	for _, run := range state.Runs() {
		usage += costOf(run.Usage)
		priced = priced || run.Usage.CostUSD != nil
	}
	if got := host.GetTotal().GetCostUsd(); !approx(got, usage) || (host.GetTotal().CostUsd != nil) != priced {
		s.fail("the host cost view totals %v (priced %v), the runs' usage is %v (priced %v)", got, host.GetTotal().CostUsd != nil, usage, priced)
	}
	if host.GetRunCount() != uint32(len(state.Runs())) {
		s.fail("the host cost view counts %d runs, the host holds %d", host.GetRunCount(), len(state.Runs()))
	}
	s.tally["host cost checked"]++
}

// finish stops every run and lets every timer and stuck turn run out, then checks the whole.
func (s *sim) finish() {
	s.clearFaults()
	s.rig.Sidecars.LaunchFailures = 0
	s.rig.Sidecars.AttachFails = false
	for _, run := range s.rig.Daemon.State().Runs() {
		if run.Status.Final() {
			continue
		}
		s.record(&pb.Command{Id: fmt.Sprintf("final-%s", run.ID), Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run.ID)}}})
	}
	if s.rig.Sidecars.Unbounded > 0 {
		s.fail("%d calls to the world were made with no bound", s.rig.Sidecars.Unbounded)
	}
	for range 20 {
		s.settle()
		for turn, se := range s.stuck {
			se.EmitEvents(&pb.SessionEvent{Body: &pb.SessionEvent_TurnEnded{TurnEnded: &pb.TurnEnded{TurnId: turn, Status: pb.TurnStatus_TURN_STATUS_COMPLETED}}})
			delete(s.stuck, turn)
		}
		s.settle()
		s.rig.Clock.Advance(core.StopGrace + time.Second)
		s.settle()
		done := true
		for _, run := range s.rig.Daemon.State().Runs() {
			done = done && run.Status.Final()
		}
		if done {
			break
		}
	}
	state := s.rig.Daemon.State()
	for _, run := range state.Runs() {
		if !run.Status.Final() {
			s.fail("run %s is %s after everything was stopped", run.ID, run.Status)
		}
	}
	if v := state.Violations(); len(v) > 0 {
		s.fail("invariants broken: %v", v)
	}
	// A run that is over has no sidecar left running, whatever its sessions were doing when it ended
	// and wherever the daemon was killed meanwhile.
	for _, se := range s.rig.Sidecars.Sessions() {
		if !se.Dead() {
			s.fail("the sidecar of %s/%d still runs after every run is over", se.Launch.Session.Agent, se.Launch.Session.N)
		}
	}
	recovered, err := s.rig.Store.Recover()
	if err != nil {
		s.fail("recover: %v", err)
	}
	if !recovered.Equal(state) {
		s.fail("the state recovered from the store differs from the live state")
	}
	if err := s.rig.Store.Verify(); err != nil {
		s.fail("verify: %v", err)
	}
	s.checkCost()
	for i, w := range s.runs {
		s.poll(w)
		s.compare(i, w)
	}
	// A client that connects now and one that stayed all along hold the same picture.
	fresh := s.openRuns(0, view{})
	s.compare(-1, fresh)
	// Its snapshot holds every message and turn the store has, of every run, whatever the faults did.
	var wantMessages []*pb.Message
	var wantTurns []*pb.Turn
	for _, run := range s.rig.Daemon.State().Runs() {
		messages, turns := storedHistory(s.t, s.rig, run.ID)
		wantMessages, wantTurns = append(wantMessages, messages...), append(wantTurns, turns...)
	}
	if !sameSet(fresh.v.messages, wantMessages) || !sameSet(fresh.v.turns, wantTurns) {
		s.fail("a snapshot holds %d messages and %d turns, the store holds %d and %d", len(fresh.v.messages), len(fresh.v.turns), len(wantMessages), len(wantTurns))
	}
	// A fold of the changes a watcher was sent holds what that snapshot says.
	for i, w := range s.runs {
		s.compareFold(i, w, fresh)
	}
	for id, iw := range s.agents {
		msgs, err := iw.w.Poll()
		if err != nil {
			s.fail("poll %s: %v", id, err)
		}
		s.feedItems(iw, msgs)
		page, err := s.rig.Store.Items(id, 0, 10)
		if err != nil {
			s.fail("items of %s: %v", id, err)
		}
		for _, it := range page.Items {
			got := iw.items[string(it.ID)]
			if got == nil || got.GetStatus() == pb.ItemStatus_ITEM_STATUS_RUNNING && it.Status != core.ItemRunning {
				s.fail("the item watcher of %s holds %v for item %s, the store has %v", id, got, it.ID, it)
			}
		}
	}
}

// sameSet says two lists hold the same messages, in the same order.
func sameSet[M proto.Message](a, b []M) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if !proto.Equal(a[i], b[i]) {
			return false
		}
	}
	return true
}

// compareFold holds a watcher's fold to a fresh snapshot, unless a snapshot left history out. A
// watcher that opened after a sequence and saw no snapshot holds only what it was sent, so it is held
// to agree on what it has.
func (s *sim) compareFold(i int, w *simWatcher, fresh *simWatcher) {
	if w.v.truncated || fresh.v.truncated {
		return
	}
	for id, got := range w.v.foldedTurns {
		if want, ok := fresh.v.foldedTurns[id]; !ok || !proto.Equal(got, want) {
			s.fail("watcher %d folded turn %v, a snapshot says %v", i, got, want)
		}
	}
	for id, got := range w.v.foldedMessages {
		if want, ok := fresh.v.foldedMessages[id]; !ok || !proto.Equal(got, want) {
			s.fail("watcher %d folded message %v, a snapshot says %v", i, got, want)
		}
	}
	if w.v.foldedComplete && (len(w.v.foldedTurns) != len(fresh.v.foldedTurns) || len(w.v.foldedMessages) != len(fresh.v.foldedMessages)) {
		s.fail("watcher %d folded %d turns and %d messages, a snapshot holds %d and %d", i, len(w.v.foldedTurns), len(w.v.foldedMessages), len(fresh.v.foldedTurns), len(fresh.v.foldedMessages))
	}
}

func (s *sim) compare(i int, w *simWatcher) {
	state := s.rig.Daemon.State()
	for _, run := range state.Runs() {
		got := w.v.runs[string(run.ID)]
		want := wireRun(run)
		if got == nil || !proto.Equal(got, want) {
			s.fail("watcher %d holds run %v, the daemon has %v", i, got, want)
		}
		for _, a := range state.Agents(run.ID) {
			if got := w.v.agents[string(a.ID)]; got == nil || !proto.Equal(got, wireAgent(a)) {
				s.fail("watcher %d holds agent %v, the daemon has %v", i, got, wireAgent(a))
			}
		}
	}
	if w.v.seq != s.rig.Daemon.Last() {
		s.fail("watcher %d is at sequence %d after settling, the log is at %d", i, w.v.seq, s.rig.Daemon.Last())
	}
}

func seeds() []uint64 {
	if v := os.Getenv("ORCHESTRATOR_SIM_SEED"); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			panic(err)
		}
		return []uint64{n}
	}
	count := 40
	if v := os.Getenv("ORCHESTRATOR_SIM_SEEDS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			panic(err)
		}
		count = n
	}
	out := make([]uint64, count)
	for i := range out {
		out[i] = uint64(i + 1)
	}
	return out
}

// TestDaemonSimulation runs whole daemons from seeds. To replay one:
//
//	ORCHESTRATOR_SIM_SEED=7 go test ./internal/daemon -run 'TestDaemonSimulation$'
//
// To search further than the default 40 seeds, set ORCHESTRATOR_SIM_SEEDS.
func TestDaemonSimulation(t *testing.T) {
	total := counter{}
	for _, seed := range seeds() {
		s := &sim{
			t: t, seed: seed, rng: rand.New(rand.NewPCG(seed, 99)), stuck: map[string]*daemontest.Session{},
			agents: map[core.AgentID]*itemWatcher{}, tally: counter{},
		}
		s.rig = daemontest.NewRig(t)
		if seed%3 == 0 {
			// A tight hub makes clients that stop reading fall behind their bounds.
			s.rig.Limits = hub.Limits{MaxItems: 6 + int(seed%5), MaxBytes: 4000}
		}
		s.init()
		for i := 0; i < 150; i++ {
			s.step()
			if v := s.rig.Daemon.State().Violations(); len(v) > 0 {
				s.fail("invariants broken: %v", v)
			}
		}
		s.finish()
		for _, run := range s.rig.Daemon.State().Runs() {
			s.tally["lead reminder"] += int(run.LeadReminders)
		}
		s.tally["input changed nothing"] += s.noops
		for k, v := range s.tally {
			total[k] += v
		}
	}
	if len(seeds()) > 1 {
		for _, k := range []string{"restart", "reconnect", "retry", "conflict", "sidecar death", "bad token refused", "stale session refused", "snapshot", "input changed nothing", "stalled call", "restart alone checked", "delivery checked", "job count checked", "cost checked", "lead reminder", "crash " + daemontest.CrashAfterClaim, "crash " + daemontest.CrashBeforeComplete, "crash after send"} {
			if total[k] == 0 {
				t.Errorf("no seed reached %q: the simulation never exercised it (reached: %v)", k, total)
			}
		}
	}
}

// handoffBy is a handoff with the body of the role that posts it.
func handoffBy(role core.AgentRole) *pb.Handoff {
	h := &pb.Handoff{Claim: "done"}
	switch role {
	case core.RolePlanner:
		h.Body = &pb.Handoff_Planner{Planner: &pb.PlannerHandoff{Plan: "p"}}
	case core.RoleJudge:
		h.Body = &pb.Handoff_Judge{Judge: &pb.JudgeHandoff{Commit: "c", Verdict: pb.JudgeVerdict_JUDGE_VERDICT_PASS, Reasons: "r"}}
	case core.RoleCombiner:
		h.Body = &pb.Handoff_Combiner{Combiner: &pb.CombinerHandoff{Commit: "c", Branch: "b", Applied: "a"}}
	case core.RoleProfiler:
		h.Body = &pb.Handoff_Profiler{Profiler: &pb.ProfilerHandoff{Question: "q", Revision: "r", Answer: "a"}}
	default:
		h.Body = &pb.Handoff_Worker{Worker: &pb.WorkerHandoff{Outcome: &pb.WorkerHandoff_NoCandidate{NoCandidate: &pb.NoCandidate{Blocker: "none"}}}}
	}
	return h
}
