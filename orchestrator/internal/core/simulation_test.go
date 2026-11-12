package core_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"os"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/jobs/jobstest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// The simulation runs whole runs against a model of everything around the core, in one thread:
// a daemon that crashes and replays its log, an outbox carried out at least once, sidecars that
// outlive the daemon and die on their own, clients that retry, and a job source that repeats
// itself. One seed fixes everything. Nothing here reads a clock or waits.
//
// It decides nothing itself about right and wrong: what it checks is in monitor, computed from the
// committed log, and in State.Violations.

// seedEnv replays one seed: see TestSimulation.
const seedEnv = "ORCHESTRATOR_SIM_SEED"

// session is the model of one sidecar. It outlives the daemon and keeps its whole stream, so that a
// daemon that comes back reads on from its last commit.
type session struct {
	id     core.Session
	caps   core.Capabilities
	dead   bool
	closed bool
	// stream holds what the session has said; each fact's offset is its index plus one.
	stream []core.FactBody
	// turn is the turn it is running, and items how many items that turn has shown.
	turn  core.TurnID
	items int
	// seen holds the effect ids carried out, as a real sidecar would to drop a repeat.
	seen map[core.EffectID]bool
	// opening says the session was launched and has not said whether it opened: only a later word
	// from it, or the launch timer, ends the wait.
	opening bool
}

func (s *session) say(f core.FactBody) { s.stream = append(s.stream, f) }

// kept is a state from earlier in a run. Decide must leave it as it was, whatever came after.
type kept struct {
	state   core.State
	commits int
}

// commit is one durable transaction, as the store will hold it.
type commit struct {
	events  [][]byte
	effects [][]byte
}

type sim struct {
	t   *testing.T
	r   *rand.Rand
	now time.Time

	// The durable half survives a crash: the log, the outbox, the armed timers.
	log    []commit
	outbox [][]byte

	// The volatile half is rebuilt from it.
	state core.State
	// read is how far each session's stream has been read since the last restart.
	read map[core.Session]int

	sessions map[core.Session]*session
	timers   []core.ArmTimer
	jobs     *jobstest.FakeJobSource
	// jobIDs counts the jobs set for each run. A real job id is only unique within one task's store,
	// so every run's first job is called j1, and the core has to tell runs apart by more than the id.
	jobIDs map[core.RunID]int
	// retries holds commands a client has sent and may send again.
	retries []core.Command
	// silent holds sessions that will not answer a close, which only the grace timer ends.
	silent map[core.Session]bool
	kept   []kept
	caps   map[core.AgentID]core.Capabilities
	plan   []action
	// slowStarts says some launches of this seed do not open at once.
	slowStarts bool

	inputs  int
	nonce   int
	trace   []string
	monitor *monitor
	seed    uint64
}

func newSim(t *testing.T, seed uint64) *sim {
	w := &sim{
		t: t, r: rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15)), now: epoch, seed: seed,
		read: map[core.Session]int{}, sessions: map[core.Session]*session{}, jobs: jobstest.NewFakeJobSource(), jobIDs: map[core.RunID]int{},
		silent: map[core.Session]bool{}, caps: map[core.AgentID]core.Capabilities{}, monitor: newMonitor(),
	}
	w.plan = w.drawPlan()
	return w
}

func (w *sim) fail(format string, args ...any) {
	w.t.Helper()
	tail := w.trace
	if len(tail) > 40 {
		tail = tail[len(tail)-40:]
	}
	w.t.Fatalf("seed %d failed: %s\nreplay: %s=%d go test ./internal/core -run 'TestSimulation$'\nlast steps:\n  %s",
		w.seed, fmt.Sprintf(format, args...), seedEnv, w.seed, strings.Join(tail, "\n  "))
}

func (w *sim) note(format string, args ...any) {
	w.trace = append(w.trace, fmt.Sprintf(format, args...))
}

// decide is the shell: one input, one atomic commit of its events and effects.
func (w *sim) decide(body core.InputBody) {
	w.t.Helper()
	w.inputs++
	w.nonce++
	// An input arrives at any instant the daemon's clock reads, not on a whole second, so a duration
	// the core works out from it meets the wire's whole seconds on the way to the log.
	w.now = w.now.Add(time.Duration(1+w.r.IntN(5))*time.Second + time.Duration(w.r.IntN(1000))*time.Millisecond)
	// The job tool the shell answers with changes between starts, as it does between daemon starts; a
	// run keeps the one in force when it started.
	if command, ok := body.(core.Command); ok {
		if start, ok := command.Body.(core.StartRun); ok {
			start.JobTool = core.JobToolEndpoint{Executable: fmt.Sprintf("/cli/%d", w.inputs%3), RuntimeDirectory: fmt.Sprintf("/run/%d", w.inputs%2)}
			command.Body = start
			body = command
		}
	}
	in := core.Input{At: w.now, Nonce: core.Nonce(fmt.Sprintf("%d-%d", w.seed, w.nonce)), Body: body}
	w.note("%d input %T %+v", w.inputs, body, body)
	before := w.state
	var events []core.Event
	var effects []core.Effect
	w.state, events, effects = core.Decide(w.state, in)
	if len(events) == 0 && !reflect.DeepEqual(before, w.state) {
		w.fail("an input with no events changed the state")
	}
	if len(events) == 0 && len(effects) > 0 {
		w.fail("an input with no events asked for %v", effects)
	}
	if len(events) > 0 {
		var c commit
		for _, e := range events {
			w.note("    event %T %+v", e.Body, e.Body)
			data, err := wire.EncodeEvent(uint64(len(w.log)+1), e)
			if err != nil {
				w.fail("encode %+v: %v", e, err)
			}
			c.events = append(c.events, data)
		}
		for _, e := range effects {
			w.note("    effect %T %+v", e, e)
			data, err := wire.EncodeEffect(e)
			if err != nil {
				w.fail("encode %+v: %v", e, err)
			}
			c.effects = append(c.effects, data)
		}
		w.log = append(w.log, c)
		w.outbox = append(w.outbox, c.effects...)
	}
	if err := w.monitor.commit(body, events, effects); err != nil {
		w.fail("%v", err)
	}
	if v := w.state.Violations(); len(v) > 0 {
		w.fail("after input %d: %v", w.inputs, v)
	}
	// Not in State.Violations: a log written before the rule may hold two, and must replay as written.
	// This simulation starts empty, so every run in it was decided under the rule.
	held := map[string]core.RunID{}
	for _, run := range w.state.Runs() {
		if other, twice := held[run.Repository]; twice && !run.Status.Final() {
			w.fail("after input %d: runs %s and %s are both not over in %s", w.inputs, other, run.ID, run.Repository)
		}
		if !run.Status.Final() {
			held[run.Repository] = run.ID
		}
	}
	if !reflect.DeepEqual(core.Replay(before, events), w.state) {
		w.fail("replaying input %d's events differs from what Decide returned", w.inputs)
	}
}

// crash loses everything volatile. The next daemon replays the log through the bytes the store would
// hold, and its state must equal the state that was live. Sidecars and the outbox are not touched.
func (w *sim) crash() {
	w.t.Helper()
	w.note("%d crash", w.inputs)
	live := w.state
	for _, run := range live.Runs() {
		if run.PendingLead != nil {
			w.monitor.covered.updates.crashedPending++
		}
	}
	w.state = core.Replay(core.State{}, w.decodeLog())
	if !reflect.DeepEqual(live, w.state) {
		w.fail("the replayed state differs from the live one\nlive:   %+v\nreplay: %+v", live, w.state)
	}
	if !live.Equal(w.state) {
		w.fail("Equal calls the replayed state different from the live one")
	}
	w.checkCheckpoint(live)
	if len(w.kept) < 4 {
		w.kept = append(w.kept, kept{state: w.state, commits: len(w.log)})
	}
	w.read = map[core.Session]int{}
	for id := range w.sessions {
		w.read[id] = int(w.state.CommittedOffset(id))
	}
}

// checkCheckpoint holds a state's checkpoint to what it promises: the events pass the contract, and
// replaying them from nothing gives a state equal to the one they describe.
func (w *sim) checkCheckpoint(s core.State) {
	w.t.Helper()
	var events []core.Event
	for i, e := range s.Checkpoint() {
		data, err := wire.EncodeEvent(uint64(i+1), e)
		if err != nil {
			w.fail("encode a checkpoint event %+v: %v", e, err)
		}
		_, back, err := wire.DecodeEvent(data)
		if err != nil {
			w.fail("decode a checkpoint event %+v: %v", e, err)
		}
		events = append(events, back)
	}
	if got := core.Replay(core.State{}, events); !got.Equal(s) {
		w.fail("a checkpoint replays to a different state\nlive:       %+v\ncheckpoint: %+v", s, got)
	}
	if v := core.Replay(core.State{}, events).Violations(); len(v) > 0 {
		w.fail("the state from a checkpoint breaks %v", v)
	}
}

func (w *sim) decodeLog() []core.Event { return w.decodeCommits(len(w.log)) }

func (w *sim) decodeCommits(n int) []core.Event {
	w.t.Helper()
	var out []core.Event
	for _, c := range w.log[:n] {
		for _, data := range c.events {
			_, e, err := wire.DecodeEvent(data)
			if err != nil {
				w.fail("decode a committed event: %v", err)
			}
			out = append(out, e)
		}
	}
	return out
}

// command sends a new command, and keeps it for a retry.
func (w *sim) command(caller core.Caller, m *pb.Command) {
	w.t.Helper()
	w.nonce++
	m.Id = fmt.Sprintf("cmd-%d", w.nonce)
	data, err := proto.Marshal(m)
	if err != nil {
		w.fail("marshal: %v", err)
	}
	c, err := wire.ParseCommand(data, caller)
	if err != nil {
		w.fail("a well-formed command did not parse: %v", err)
	}
	w.retries = append(w.retries, c)
	w.decide(c)
}

func (w *sim) retry() {
	if len(w.retries) == 0 {
		return
	}
	c := w.retries[w.r.IntN(len(w.retries))]
	first, recall := w.state.Recall(c.ID, c.Digest)
	if recall != core.RecallRepeat {
		w.fail("command %s was not remembered: %s", c.ID, recall)
	}
	w.decide(c)
	if again, _ := w.state.Recall(c.ID, c.Digest); !reflect.DeepEqual(again, first) {
		w.fail("a retry of %s changed the receipt from %+v to %+v", c.ID, first, again)
	}
}

func pick[T any](w *sim, from []T) (T, bool) {
	if len(from) == 0 {
		var none T
		return none, false
	}
	return from[w.r.IntN(len(from))], true
}

func (w *sim) liveRuns() []core.Run {
	return slices.DeleteFunc(w.state.Runs(), func(r core.Run) bool { return !r.Status.Live() })
}

func (w *sim) agents(run core.RunID, keep func(core.Agent) bool) []core.Agent {
	return slices.DeleteFunc(w.state.Agents(run), func(a core.Agent) bool { return !keep(a) })
}

func subagent(a core.Agent) bool { return a.Role != core.RoleLead }

func (w *sim) startRun() {
	start := &pb.StartRun{
		Repository: fmt.Sprintf("repo-%d", w.r.IntN(3)), Task: "task", FanoutLimit: uint32(1 + w.r.IntN(4)), Brief: "go",
		Lead: &pb.AgentSpec{Harness: pb.Harness(1 + w.r.IntN(2)), Model: "m"},
	}
	for range w.r.IntN(3) {
		start.AllowAgents = append(start.AllowAgents, &pb.AllowedAgent{Harness: pb.Harness(1 + w.r.IntN(2)), Model: []string{"", "m", "x"}[w.r.IntN(3)]})
	}
	if w.r.IntN(2) == 0 {
		start.MaxCostUsd = &[]float64{1, 3, 10}[w.r.IntN(3)]
	}
	if w.r.IntN(2) == 0 {
		start.MaxTime = durationpb.New(time.Duration(300+w.r.IntN(1800)) * time.Second)
	}
	// Most runs ask their lead for a report every few minutes, which the pauses of the simulation outlast;
	// the rest take the default of an hour.
	if w.r.IntN(4) != 0 {
		start.SummaryEvery = durationpb.New(time.Duration(5+w.r.IntN(16)) * time.Minute)
	}
	w.command(core.Operator{}, &pb.Command{Body: &pb.Command_StartRun{StartRun: start}})
}

// startAnotherRun keeps a few runs going at once, so that they share nothing by accident.
func (w *sim) startAnotherRun() {
	if len(w.liveRuns()) < 3 {
		w.startRun()
	}
}

func (w *sim) spawn() {
	run, ok := pick(w, w.liveRuns())
	if !ok {
		return
	}
	role := pb.AgentRole(2 + w.r.IntN(5))
	w.command(w.callerIn(run), &pb.Command{Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{
		RunId: string(run.ID), Role: role, Brief: "work",
		Spec: &pb.AgentSpec{Harness: pb.Harness(w.r.IntN(3)), Model: []string{"", "m", "x"}[w.r.IntN(3)]},
	}}})
}

// callerIn is the lead of a run if it is live, and the operator otherwise.
func (w *sim) callerIn(run core.Run) core.Caller {
	if a, ok := w.state.Agent(run.Lead); ok && !a.Status.Over() && w.r.IntN(2) == 0 {
		return core.AgentCaller{Agent: a.ID}
	}
	return core.Operator{}
}

func (w *sim) send() {
	run, ok := pick(w, w.liveRuns())
	if !ok {
		return
	}
	agents := w.state.Agents(run.ID)
	a, ok := pick(w, agents)
	if !ok {
		return
	}
	w.command(w.callerIn(run), &pb.Command{Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{
		AgentId: string(a.ID), Text: "hello",
	}}})
}

func (w *sim) handoff() {
	run, ok := pick(w, w.liveRuns())
	if !ok {
		return
	}
	anyTime := w.r.IntN(4) == 0
	a, ok := pick(w, w.agents(run.ID, func(a core.Agent) bool {
		return subagent(a) && !a.Status.Over() && (anyTime || a.Status == core.AgentActive)
	}))
	if !ok {
		return
	}
	w.command(core.AgentCaller{Agent: a.ID}, &pb.Command{Body: &pb.Command_PostHandoff{PostHandoff: &pb.PostHandoff{
		Handoff: handoffBy(a.Role),
	}}})
}

func (w *sim) stopSubagent() {
	run, ok := pick(w, w.liveRuns())
	if !ok {
		return
	}
	a, ok := pick(w, w.agents(run.ID, subagent))
	if !ok {
		return
	}
	w.command(w.callerIn(run), &pb.Command{Body: &pb.Command_StopAgent{StopAgent: &pb.StopAgent{AgentId: string(a.ID)}}})
}

func (w *sim) stopRun() {
	run, ok := pick(w, w.state.Runs())
	if !ok {
		return
	}
	w.command(core.Operator{}, &pb.Command{Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run.ID)}}})
}

// jobUpdate moves a job along and polls the source, which repeats every job it knows.
func (w *sim) pollJobs() {
	run, ok := pick(w, w.state.Runs())
	if !ok {
		return
	}
	switch w.r.IntN(3) {
	case 0:
		w.jobIDs[run.ID]++
		w.jobs.Set(core.JobUpdate{Job: core.JobID(fmt.Sprintf("j%d", w.jobIDs[run.ID])), Run: run.ID, State: core.JobRunning})
	case 1:
		if w.jobIDs[run.ID] > 0 {
			states := []core.JobState{core.JobSucceeded, core.JobFailed, core.JobCancelled, core.JobLost}
			w.jobs.Set(core.JobUpdate{
				Job: core.JobID(fmt.Sprintf("j%d", 1+w.r.IntN(w.jobIDs[run.ID]))), Run: run.ID, State: states[w.r.IntN(len(states))],
			})
		}
	default:
		w.jobs.FailNext()
	}
	updates, err := w.jobs.Jobs(context.Background(), run)
	if err != nil {
		return
	}
	var going uint32
	for _, u := range updates {
		w.decide(u)
		if !u.State.Final() {
			going++
		}
	}
	w.decide(core.JobsRunning{Run: run.ID, Count: going})
}

// report has a lead post a report of either kind.
func (w *sim) report() {
	run, ok := pick(w, w.liveRuns())
	if !ok {
		return
	}
	kind := pb.ReportKind(1 + w.r.IntN(2))
	w.command(core.AgentCaller{Agent: run.Lead}, &pb.Command{Body: &pb.Command_PostReport{PostReport: &pb.PostReport{
		Report: &pb.Report{Kind: kind, Headline: "h"},
	}}})
}

// press has a viewer, one of three installs or sometimes none, press Catch me up on a run that is
// live or over. A press that comes again with the same command id is a retry, which the simulation
// sends at random like any other.
func (w *sim) press() {
	run, ok := pick(w, w.state.Runs())
	if !ok {
		return
	}
	var viewer core.Viewer
	if n := w.r.IntN(13); n < 12 {
		viewer = core.Viewer{ID: core.ViewerID(fmt.Sprintf("install-%d", n%3)), Label: fmt.Sprintf("viewer %d", n%3)}
	}
	w.command(core.Operator{Viewer: viewer}, &pb.Command{Body: &pb.Command_RequestCatchUp{RequestCatchUp: &pb.RequestCatchUp{RunId: string(run.ID)}}})
}

// answerWindow is the far side of ComputeWindowFacts: the shell works the facts out and says so, or
// cannot. The effect is carried out at least once, so the answer comes more than once, and a later one
// says something else; the check-in keeps the first.
func (w *sim) answerWindow(e core.ComputeWindowFacts) {
	if _, ok := w.state.CheckIn(e.CheckIn); !ok {
		w.fail("asked to work out the facts of %s, which is not a check-in", e.CheckIn)
	}
	if w.r.IntN(5) == 0 {
		w.decide(core.WindowFactsFailed{CheckIn: e.CheckIn, Detail: fmt.Sprintf("the job log could not be read (%d)", w.r.IntN(100))})
		return
	}
	w.decide(core.WindowFactsReady{CheckIn: e.CheckIn, Facts: drawnFacts(w.r.IntN)})
}

// pause lets a long time pass with nothing happening, which is what a stall needs, and then fires
// the timers that came due.
func (w *sim) pause() {
	w.now = w.now.Add(time.Duration(1+w.r.IntN(15)) * time.Minute)
	for range 3 {
		w.fireTimer(false)
	}
}

// lull lets everything in flight finish, so that agents rest and mail is delivered, and then pauses
// for longer than a lead may be left alone. Runs that were busy only because of what was in flight
// are standing still, which a random mix of actions seldom leaves for long.
func (w *sim) lull() {
	for range 300 {
		switch {
		case len(w.outbox) > 0:
			w.work()
		case w.readSidecar():
		default:
			ended := false
			for _, s := range w.openSessions() {
				if s.turn != "" {
					w.endTurn(s, core.TurnCompleted)
					ended = true
				}
			}
			if !ended {
				w.reapSidecars()
				w.now = w.now.Add(core.LeadIdleAfter + time.Duration(w.r.IntN(60))*time.Second)
				for range 3 {
					w.fireTimer(false)
				}
				return
			}
		}
		w.reapSidecars()
	}
}

// work carries out one effect from the outbox. An effect stays there at random, so it runs again,
// which is the at-least-once the outbox promises.
func (w *sim) work() {
	if len(w.outbox) == 0 {
		return
	}
	i := w.r.IntN(min(len(w.outbox), 3))
	effect, err := wire.DecodeEffect(w.outbox[i])
	if err != nil {
		w.fail("decode an effect: %v", err)
	}
	if w.r.IntN(4) != 0 {
		w.outbox = slices.Delete(w.outbox, i, i+1)
	}
	w.note("%d effect %T %+v", w.inputs, effect, effect)
	w.carryOut(effect)
}

// capabilities are the harness's, so they stay the same across an agent's sessions.
func (w *sim) capabilities(agent core.AgentID) core.Capabilities {
	c, ok := w.caps[agent]
	if !ok {
		c = core.Capabilities{Steers: w.r.IntN(2) == 0, Resumes: w.r.IntN(4) != 0}
		w.caps[agent] = c
	}
	return c
}

// carryOut is the far side of the effects. A repeat changes nothing, as the contract says.
func (w *sim) carryOut(e core.Effect) {
	var id core.Session
	switch b := e.(type) {
	case core.ArmTimer:
		if !slices.Contains(w.timers, b) {
			w.timers = append(w.timers, b)
		}
		return
	case core.ComputeWindowFacts:
		w.answerWindow(b)
		return
	case core.ReleaseCheckouts:
		// Working copies are removed only once nothing can continue in them, and the answer names some
		// of them as kept.
		if run, ok := w.state.Run(b.Run); !ok || !run.Status.Final() {
			w.fail("asked to release the checkouts of %s while it is %q", b.Run, run.Status)
		}
		var kept []core.Checkout
		for _, a := range w.state.Agents(b.Run) {
			if a.Role != core.RoleLead && w.r.IntN(2) == 0 {
				kept = append(kept, core.Checkout{Agent: a.ID, Location: "/state/" + string(a.ID) + "/worktree"})
			}
		}
		w.decide(core.CheckoutsReleased{Run: b.Run, Kept: kept})
		return
	case core.LaunchSession:
		if _, ok := w.state.Run(b.Run); !ok {
			w.fail("launch %+v names no run", b)
		}
		if _, ok := w.sessions[b.Session]; ok {
			return
		}
		s := &session{id: b.Session, caps: w.capabilities(b.Session.Agent), seen: map[core.EffectID]bool{}}
		w.sessions[b.Session] = s
		if b.ResumeToken != "" && !strings.HasPrefix(b.ResumeToken, "conv-") {
			w.fail("a resume got conversation %q", b.ResumeToken)
		}
		if w.slowStarts && w.r.IntN(3) == 0 {
			s.opening = true
			return
		}
		w.open(s, b.ResumeToken)
		return
	case core.StartTurn:
		id = b.Session
	case core.Steer:
		id = b.Session
	case core.Interrupt:
		id = b.Session
	case core.CloseSession:
		id = b.Session
	}
	w.cooperate(e, id)
}

// open has a session report that it is ready.
func (w *sim) open(s *session, conversation string) {
	if conversation == "" {
		conversation = "conv-" + string(s.id.Agent)
	}
	s.opening = false
	s.say(core.SessionOpened{ResumeToken: conversation, Capabilities: s.caps})
}

// cooperate carries out a command a session is sent, as a cooperative sidecar does.
func (w *sim) cooperate(e core.Effect, id core.Session) {
	s, ok := w.sessions[id]
	if !ok || s.dead || s.closed || s.seen[e.ID()] {
		return
	}
	s.seen[e.ID()] = true
	switch b := e.(type) {
	case core.StartTurn:
		s.turn, s.items = b.Turn, 0
	case core.Steer:
		accepted := s.turn == b.Turn
		s.say(core.SteerOutcome{Message: b.Message.ID, Accepted: accepted})
	case core.Interrupt:
		if s.turn == b.Turn {
			w.endTurn(s, core.TurnInterrupted)
		}
	case core.CloseSession:
		if b.Force {
			s.dead = true
			return
		}
		if w.silent[id] {
			return
		}
		if s.turn != "" {
			w.endTurn(s, core.TurnInterrupted)
		}
		s.closed = true
		s.say(core.SessionClosed{})
	}
}

func (w *sim) endTurn(s *session, status core.TurnStatus) {
	in := uint64(10 + s.items)
	usage := core.Usage{InputTokens: &in}
	if w.r.IntN(2) == 0 {
		cost := []float64{0.25, 1, 2}[w.r.IntN(3)]
		usage.CostUSD = &cost
	}
	s.say(core.TurnEnded{Turn: s.turn, Status: status, Usage: usage})
	s.turn = ""
}

// act makes one open session do something by itself.
func (w *sim) act() {
	open := w.openSessions()
	s, ok := pick(w, open)
	if !ok {
		return
	}
	switch w.r.IntN(8) {
	case 0, 1, 2:
		if s.turn != "" {
			s.items++
			status := core.ItemRunning
			if w.r.IntN(2) == 0 {
				status = core.ItemFinished
			}
			s.say(core.ItemObserved{
				ID: core.ItemID(fmt.Sprintf("%s-item-%d", s.turn, 1+w.r.IntN(s.items))), Turn: s.turn, Status: status,
				Body: core.ToolCall{Use: core.ShellUse{Command: "ls"}, Output: "x"},
			})
		}
	case 3:
		if s.turn != "" {
			cost := 0.5
			s.say(core.UsageReported{Turn: s.turn, Usage: core.Usage{CostUSD: &cost}})
		}
	case 4, 5:
		if s.turn != "" {
			w.endTurn(s, core.TurnCompleted)
		}
	case 6:
		if s.turn != "" && w.r.IntN(2) == 0 {
			w.endTurn(s, core.TurnFailed)
		}
	default:
		if w.r.IntN(3) == 0 {
			s.closed = true
			s.say(core.SessionFailed{Detail: "provider error"})
		}
	}
}

func (w *sim) openSessions() []*session {
	var out []*session
	for _, id := range w.sessionIDs() {
		if s := w.sessions[id]; !s.dead && !s.closed {
			out = append(out, s)
		}
	}
	return out
}

func (w *sim) sessionIDs() []core.Session {
	out := make([]core.Session, 0, len(w.sessions))
	for id := range w.sessions {
		out = append(out, id)
	}
	slices.SortFunc(out, func(a, b core.Session) int {
		return strings.Compare(fmt.Sprint(a), fmt.Sprint(b))
	})
	return out
}

// readSidecar delivers the next fact of a session with unread facts, and sometimes one again.
func (w *sim) readSidecar() bool {
	var unread []*session
	for _, id := range w.sessionIDs() {
		if s := w.sessions[id]; w.read[id] < len(s.stream) {
			unread = append(unread, s)
		}
	}
	s, ok := pick(w, unread)
	if !ok {
		return false
	}
	offset := w.read[s.id]
	if offset > 0 && w.r.IntN(8) == 0 {
		offset--
	} else {
		w.read[s.id]++
	}
	w.decide(core.SessionFact{Session: s.id, Offset: uint64(offset + 1), Body: s.stream[offset]})
	return true
}

// killSidecar makes a sidecar die. The shell finds out when it next looks. Half the time it picks a
// lead, so that some lead dies often enough to run out of resumes.
func (w *sim) killSidecar() {
	open := w.openSessions()
	if w.r.IntN(2) == 0 {
		open = slices.DeleteFunc(open, func(s *session) bool {
			a, _ := w.state.Agent(s.id.Agent)
			return a.Role != core.RoleLead
		})
	}
	s, ok := pick(w, open)
	if !ok {
		return
	}
	w.note("%d kill %v", w.inputs, s.id)
	s.dead = true
}

// reapSidecars reports the dead sidecars whose streams have been read, which is how the shell works:
// a dead socket ends the stream, and the loss comes after what the stream held.
func (w *sim) reapSidecars() {
	for _, id := range w.sessionIDs() {
		s := w.sessions[id]
		a, ok := w.state.Agent(id.Agent)
		if s.dead && ok && a.Session == id.N && !a.Status.Over() && w.read[id] >= len(s.stream) {
			w.decide(core.SessionLost{Session: id})
		}
	}
}

func (w *sim) fireTimer(all bool) {
	due := slices.DeleteFunc(slices.Clone(w.timers), func(t core.ArmTimer) bool { return !all && t.FireAt.After(w.now) })
	t, ok := pick(w, due)
	if !ok {
		return
	}
	w.timers = slices.DeleteFunc(w.timers, func(o core.ArmTimer) bool { return o == t })
	w.now = latest(w.now, t.FireAt)
	w.decide(core.TimerFired{Timer: t})
	if w.r.IntN(4) == 0 {
		w.decide(core.TimerFired{Timer: t})
	}
}

func latest(a, b time.Time) time.Time {
	if b.After(a) {
		return b
	}
	return a
}

// action is one kind of thing that can happen, with its weight in a thousand.
type action struct {
	weight int
	run    func()
}

// drawPlan fixes how often each thing happens for a whole seed. Each fault comes at none, one or
// two times its weight, so that some seeds leave a fault out altogether and let the others go deep.
func (w *sim) drawPlan() []action {
	fault := func(weight int) int { return weight * w.r.IntN(3) }
	w.slowStarts = w.r.IntN(2) == 0
	return []action{
		{40, w.finishStart}, {20, w.startAnotherRun}, {100, w.spawn}, {80, w.send}, {60, w.handoff}, {40, w.pollJobs}, {20, w.report},
		{fault(10), w.pause}, {fault(8), w.lull}, {40, w.press}, {fault(30), w.update},
		{fault(10), w.stopSubagent}, {fault(2), w.stopRun}, {fault(15), w.retry}, {fault(5), w.crash},
		{fault(20), w.killSidecar}, {fault(5), w.silence}, {20, func() { w.fireTimer(false) }},
		{200, w.work}, {160, w.act}, {230, func() { w.readSidecar() }},
	}
}

// startFailures are the reasons a session that cannot open gives.
var startFailures = []core.StartFailure{
	core.StartAgentNotInstalled, core.StartNotSignedIn, core.StartPluginUnavailable, core.StartToolsUnavailable,
	core.StartWorkingDirUnavailable, core.StartOther,
}

// finishStart has a session that was slow to open say how it went: it opens, or fails to.
func (w *sim) finishStart() {
	var waiting []*session
	for _, s := range w.openSessions() {
		if s.opening {
			waiting = append(waiting, s)
		}
	}
	s, ok := pick(w, waiting)
	if !ok {
		return
	}
	if w.r.IntN(3) == 0 {
		s.opening = false
		s.say(core.SessionFailed{Detail: "could not start the agent", StartFailure: startFailures[w.r.IntN(len(startFailures))]})
		return
	}
	w.open(s, "")
}

func (w *sim) silence() {
	if s, ok := pick(w, w.openSessions()); ok {
		w.silent[s.id] = true
	}
}

// step makes one random thing happen.
func (w *sim) step() {
	total := 0
	for _, a := range w.plan {
		total += a.weight
	}
	n := w.r.IntN(total)
	for _, a := range w.plan {
		if n -= a.weight; n < 0 {
			a.run()
			break
		}
	}
	w.reapSidecars()
}

// settle stops everything, heals what is left, and runs until nothing is pending: every run must
// reach a final status and every message a final delivery, or something waits forever.
func (w *sim) settle() {
	w.t.Helper()
	w.crash()
	for _, run := range w.state.Runs() {
		if run.Status.Live() {
			w.command(core.Operator{}, &pb.Command{Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run.ID)}}})
		}
	}
	for range 100000 {
		switch {
		case len(w.outbox) > 0:
			w.work()
		case w.readSidecar():
		case len(w.timers) > 0:
			w.fireTimer(true)
		default:
			for _, s := range w.openSessions() {
				if s.turn != "" {
					w.endTurn(s, core.TurnCompleted)
				}
			}
			w.reapSidecars()
			if len(w.outbox) == 0 && w.quiet() {
				w.finalChecks()
				return
			}
		}
		w.reapSidecars()
	}
	w.fail("the system did not settle")
}

func (w *sim) quiet() bool {
	if len(w.timers) > 0 {
		return false
	}
	for _, id := range w.sessionIDs() {
		if w.read[id] < len(w.sessions[id].stream) {
			return false
		}
	}
	return true
}

func (w *sim) finalChecks() {
	w.t.Helper()
	for _, run := range w.state.Runs() {
		if !run.Status.Final() {
			w.fail("run %s never finished: %+v\nagents: %+v", run.ID, run, w.state.Agents(run.ID))
		}
		for _, a := range w.state.Agents(run.ID) {
			if !a.Status.Over() {
				w.fail("agent %s of a finished run is %s", a.ID, a.Status)
			}
		}
	}
	if pending := w.state.InTransit(); len(pending) > 0 {
		w.fail("messages never settled: %+v", pending)
	}
	if err := w.monitor.final(); err != nil {
		w.fail("%v", err)
	}
	w.crash()
	for _, k := range w.kept {
		if !reflect.DeepEqual(k.state, core.Replay(core.State{}, w.decodeCommits(k.commits))) {
			w.fail("a later Decide changed the state as of commit %d", k.commits)
		}
	}
}

func simulate(t *testing.T, seed uint64, steps int) *monitor {
	t.Helper()
	w := newSim(t, seed)
	w.startRun()
	for range steps {
		w.step()
	}
	w.settle()
	return w.monitor
}

// TestSimulation runs whole runs with random crashes, restarts, retries and sidecar deaths. To
// replay a failing seed, set ORCHESTRATOR_SIM_SEED; the failure prints the exact command. Setting
// ORCHESTRATOR_SIM_SEEDS searches that many more.
func TestSimulation(t *testing.T) {
	const steps = 1500
	if v := os.Getenv(seedEnv); v != "" {
		seed, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			t.Fatalf("%s=%q: %v", seedEnv, v, err)
		}
		simulate(t, seed, steps)
		return
	}
	seeds := uint64(40)
	if v := os.Getenv("ORCHESTRATOR_SIM_SEEDS"); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			t.Fatalf("ORCHESTRATOR_SIM_SEEDS=%q: %v", v, err)
		}
		seeds = n
	}
	// The seeds are independent worlds, so they run side by side: the one thread of a single seed after
	// another was the longest path of the orchestrator check, and cores sat idle beside it.
	var total coverage
	var mu sync.Mutex
	t.Run("seeds", func(t *testing.T) {
		for seed := range seeds {
			t.Run(fmt.Sprint(seed), func(t *testing.T) {
				t.Parallel()
				covered := simulate(t, seed, steps).covered
				mu.Lock()
				defer mu.Unlock()
				total.add(covered)
			})
		}
	})
	total.require(t)
}

// One seed gives one run, and a run that repeats differently could not be replayed.
func TestASeedRepeatsExactly(t *testing.T) {
	first, second := newSim(t, 7), newSim(t, 7)
	for _, w := range []*sim{first, second} {
		w.startRun()
		for range 400 {
			w.step()
		}
	}
	if !reflect.DeepEqual(first.log, second.log) {
		t.Fatal("one seed produced two logs")
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
