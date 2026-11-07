package attachview

import (
	"cmp"
	"slices"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// ChangeKind says what a Change is about.
type ChangeKind int

// The things a watcher of a run is told about. A change is reported once, when the run first
// shows it.
const (
	RunMoved ChangeKind = iota
	AgentStarted
	AgentEnded
	TurnEnded
	HandoffPosted
	ReportPosted
	JobFinishedMessage
	SubagentSettled
	MessageRejected
	JobMoved
	Measured
	// RunStalled says the run now stands still: see Run.stalled_since. RunResumed says it stopped
	// standing still.
	RunStalled
	RunResumed
	// ReminderSent says the orchestrator reminded an agent of something it owes.
	ReminderSent
)

// Change is one thing that happened to the run, with the typed message that shows it.
type Change struct {
	Kind    ChangeKind
	At      time.Time
	Run     *pb.Run
	Agent   *pb.Agent
	Turn    *pb.Turn
	Message *pb.Message
	Job     *cliv1.Job
	Result  *cliv1.Result
}

// View is one run as a client sees it: the snapshot folded with the changes after it. It is the
// whole state a verb keeps, and applying the same frames in the same order always gives the same
// View and the same Changes.
type View struct {
	RunID    string
	Run      *pb.Run
	Agents   map[string]*pb.Agent
	Turns    map[string]*pb.Turn
	Messages map[string]*pb.Message
	// Search is the run's latest search view, or nil if none has arrived.
	Search *pb.SearchState
	// Sequence is the last sequence applied.
	Sequence uint64
	// Truncated says a snapshot left out part of the history, so counts over it are not the run's.
	Truncated bool
	// names is AgentName's cache, rebuilt when the set of agents changes.
	names map[string]string
}

// NewView is the empty view of a run.
func NewView(run string) *View {
	return &View{RunID: run, Agents: map[string]*pb.Agent{}, Turns: map[string]*pb.Turn{}, Messages: map[string]*pb.Message{}}
}

// Load folds a snapshot in and returns what it shows that the view did not have, oldest first. On
// an empty view that is the history of the run.
func (v *View) Load(s *pb.RunsSnapshot) []Change {
	v.Sequence = max(v.Sequence, s.GetSequence())
	v.Truncated = v.Truncated || s.GetTruncated()
	var out []Change
	for _, r := range s.GetRuns() {
		if r.GetId() == v.RunID {
			out = append(out, v.setRun(r, time.Time{})...)
		}
	}
	for _, a := range s.GetAgents() {
		if a.GetRunId() == v.RunID {
			out = append(out, v.setAgent(a, time.Time{})...)
		}
	}
	for _, t := range s.GetTurns() {
		if v.Agents[t.GetAgentId()] != nil {
			out = append(out, v.setTurn(t, time.Time{})...)
		}
	}
	for _, m := range s.GetMessages() {
		if m.GetRunId() == v.RunID {
			out = append(out, v.setMessage(m, time.Time{})...)
		}
	}
	for _, search := range s.GetSearches() {
		out = append(out, v.ApplySearch(search)...)
	}
	slices.SortStableFunc(out, func(a, b Change) int { return a.At.Compare(b.At) })
	return out
}

// Fold folds in whichever part of a classified WatchRuns frame is set, and returns its changes.
func (v *View) Fold(snapshot *pb.RunsSnapshot, change *pb.RunChange, search *pb.SearchState) []Change {
	switch {
	case snapshot != nil:
		return v.Load(snapshot)
	case change != nil:
		return v.Apply(change)
	}
	return v.ApplySearch(search)
}

// Apply folds one committed change in. A change of another run, or one the view has already passed,
// changes nothing.
func (v *View) Apply(c *pb.RunChange) []Change {
	if c.GetSequence() <= v.Sequence {
		return nil
	}
	v.Sequence = c.GetSequence()
	if c.GetRunId() != v.RunID {
		return nil
	}
	at := c.GetAt().AsTime()
	switch b := c.GetBody().(type) {
	case *pb.RunChange_Run:
		return v.setRun(b.Run, at)
	case *pb.RunChange_Agent:
		return v.setAgent(b.Agent, at)
	case *pb.RunChange_Turn:
		return v.setTurn(b.Turn, at)
	case *pb.RunChange_Message:
		return v.setMessage(b.Message, at)
	}
	return nil
}

// ApplySearch folds in a search view of this run, and returns the jobs and measurements it shows
// that the previous one did not.
func (v *View) ApplySearch(s *pb.SearchState) []Change {
	if s.GetRunId() != v.RunID {
		return nil
	}
	previous := map[string]cliv1.JobState{}
	for _, j := range v.Search.GetJobs() {
		previous[j.GetId()] = j.GetState()
	}
	measured := map[string]bool{}
	for _, r := range v.Search.GetResults() {
		measured[r.GetJobId()] = true
	}
	// A host that has just started has read nothing yet, and says so as an unreadable view with no
	// jobs. That is not the run having none: what this view already knew stays, with the new health,
	// so a count never goes down.
	if s.GetHealth().GetUnreadable() != nil && len(s.GetJobs()) == 0 && len(s.GetResults()) == 0 &&
		(len(v.Search.GetJobs()) > 0 || len(v.Search.GetResults()) > 0) {
		kept := proto.Clone(v.Search).(*pb.SearchState)
		kept.Health, kept.Revision = s.GetHealth(), s.GetRevision()
		s = kept
	}
	v.Search = s
	var out []Change
	for _, j := range s.GetJobs() {
		if state, seen := previous[j.GetId()]; !seen || state != j.GetState() {
			out = append(out, Change{Kind: JobMoved, At: jobTime(j), Job: j})
		}
	}
	for _, r := range s.GetResults() {
		if r.GetMeasure() != nil && !measured[r.GetJobId()] {
			out = append(out, Change{Kind: Measured, At: pick(r.GetRecordedAt(), time.Time{}), Result: r})
		}
	}
	return out
}

func jobTime(j *cliv1.Job) time.Time {
	for _, t := range []*timestamppb.Timestamp{j.GetFinishedAt(), j.GetStartedAt(), j.GetSubmittedAt()} {
		if t != nil {
			return t.AsTime()
		}
	}
	return time.Time{}
}

// pick is a timestamp's time, or fallback when the message has none.
func pick(t *timestamppb.Timestamp, fallback time.Time) time.Time {
	if t == nil {
		return fallback
	}
	return t.AsTime()
}

func (v *View) setRun(r *pb.Run, now time.Time) []Change {
	previous := v.Run
	v.Run = r
	out := stallChange(previous, r, now)
	if previous.GetStatus() == r.GetStatus() {
		return out
	}
	at := pick(r.GetCreatedAt(), now)
	switch {
	case previous != nil && !now.IsZero():
		at = now
	case r.GetEndedAt() != nil:
		at = pick(r.GetEndedAt(), now)
	}
	return append([]Change{{Kind: RunMoved, At: at, Run: r}}, out...)
}

// stallChange is what the run's stall shows that the previous state of the run did not: a run first
// seen already stalled is told with the time it began, and one that stops standing still is told
// with the time it was seen to.
func stallChange(previous, r *pb.Run, now time.Time) []Change {
	switch {
	case r.GetStalledSince() != nil && previous.GetStalledSince() == nil:
		return []Change{{Kind: RunStalled, At: r.GetStalledSince().AsTime(), Run: r}}
	case r.GetStalledSince() == nil && previous.GetStalledSince() != nil:
		return []Change{{Kind: RunResumed, At: now, Run: r}}
	}
	return nil
}

func (v *View) setAgent(a *pb.Agent, now time.Time) []Change {
	previous := v.Agents[a.GetId()]
	v.Agents[a.GetId()] = a
	var out []Change
	if previous == nil {
		out = append(out, Change{Kind: AgentStarted, At: pick(a.GetStartedAt(), now), Agent: a})
	}
	if agentOver(a) && !agentOver(previous) {
		out = append(out, Change{Kind: AgentEnded, At: pick(a.GetEndedAt(), now), Agent: a})
	}
	return out
}

func (v *View) setTurn(t *pb.Turn, now time.Time) []Change {
	previous := v.Turns[t.GetId()]
	v.Turns[t.GetId()] = t
	if t.GetStatus() == pb.TurnStatus_TURN_STATUS_RUNNING || (previous != nil && previous.GetStatus() != pb.TurnStatus_TURN_STATUS_RUNNING) {
		return nil
	}
	return []Change{{Kind: TurnEnded, At: pick(t.GetEndedAt(), now), Turn: t, Agent: v.Agents[t.GetAgentId()]}}
}

func (v *View) setMessage(m *pb.Message, now time.Time) []Change {
	previous := v.Messages[m.GetId()]
	v.Messages[m.GetId()] = m
	at := pick(m.GetAt(), now)
	if previous == nil {
		switch m.GetBody().(type) {
		case *pb.Message_Handoff:
			return []Change{{Kind: HandoffPosted, At: at, Message: m, Agent: v.Agents[m.GetFromAgentId()]}}
		case *pb.Message_Report:
			return []Change{{Kind: ReportPosted, At: at, Message: m, Agent: v.Agents[m.GetFromAgentId()]}}
		case *pb.Message_JobFinished:
			return []Change{{Kind: JobFinishedMessage, At: at, Message: m}}
		case *pb.Message_SubagentSettled:
			return []Change{{Kind: SubagentSettled, At: at, Message: m}}
		case *pb.Message_HandoffReminder, *pb.Message_LeadIdleReminder, *pb.Message_RunLimitReached, *pb.Message_ReportDue:
			return []Change{{Kind: ReminderSent, At: at, Message: m, Agent: v.Agents[m.GetToAgentId()]}}
		}
	}
	if m.GetDelivery() == pb.Delivery_DELIVERY_REJECTED && previous.GetDelivery() != pb.Delivery_DELIVERY_REJECTED && m.GetFromAgentId() == "" {
		return []Change{{Kind: MessageRejected, At: at, Message: m}}
	}
	return nil
}

func agentOver(a *pb.Agent) bool {
	switch a.GetStatus() {
	case pb.AgentStatus_AGENT_STATUS_ENDED, pb.AgentStatus_AGENT_STATUS_FAILED, pb.AgentStatus_AGENT_STATUS_LOST:
		return true
	}
	return false
}

// Lead is the run's lead agent, or nil before the view has it.
func (v *View) Lead() *pb.Agent { return v.Agents[v.Run.GetLeadAgentId()] }

// Started says the lead's session has opened: the lead is live, or was. A session that ended without
// opening says why in start_failure_reason, unless it was stopped while still opening: such a lead
// reads as having ended, because the run is over by then and says so.
func (v *View) Started() bool {
	lead := v.Lead()
	switch lead.GetStatus() {
	case pb.AgentStatus_AGENT_STATUS_UNSPECIFIED, pb.AgentStatus_AGENT_STATUS_LAUNCHING:
		return false
	}
	return lead.GetStartFailureReason() == pb.StartFailureReason_START_FAILURE_REASON_UNSPECIFIED
}

// Live says a run has not reached a final status.
func Live(r *pb.Run) bool {
	return r.GetStatus() != pb.RunStatus_RUN_STATUS_ENDED && r.GetStatus() != pb.RunStatus_RUN_STATUS_FAILED
}

// Over says the run has a final status.
func (v *View) Over() bool { return v.Run != nil && !Live(v.Run) }

// Reports are the reports posted in the run, oldest first: in the order the orchestrator numbered
// them.
func (v *View) Reports() []*pb.Message {
	var out []*pb.Message
	for _, m := range v.Messages {
		if m.GetReport() != nil {
			out = append(out, m)
		}
	}
	slices.SortFunc(out, func(a, b *pb.Message) int {
		if c := cmp.Compare(a.GetReport().GetSequence(), b.GetReport().GetSequence()); c != 0 {
			return c
		}
		return a.GetAt().AsTime().Compare(b.GetAt().AsTime())
	})
	return out
}

// ReportAfter is the newest report numbered above after, of the final kind only when finalOnly,
// or nil when there is none.
func (v *View) ReportAfter(after uint32, finalOnly bool) *pb.Message {
	var found *pb.Message
	for _, m := range v.Reports() {
		r := m.GetReport()
		if r.GetSequence() > after && (!finalOnly || r.GetKind() == pb.ReportKind_REPORT_KIND_FINAL) {
			found = m
		}
	}
	return found
}

// Stalled says the run stands still: see Run.stalled_since.
func (v *View) Stalled() bool { return v.Run.GetStalledSince() != nil }

// Snapshot is the view as a RunsSnapshot of this one run, in the shape the service sends.
func (v *View) Snapshot() *pb.RunsSnapshot {
	out := &pb.RunsSnapshot{Sequence: v.Sequence, Truncated: v.Truncated}
	if v.Run != nil {
		out.Runs = []*pb.Run{v.Run}
	}
	for _, a := range sortedByKey(v.Agents) {
		out.Agents = append(out.Agents, a)
	}
	for _, t := range sortedByKey(v.Turns) {
		out.Turns = append(out.Turns, t)
	}
	for _, m := range sortedByKey(v.Messages) {
		out.Messages = append(out.Messages, m)
	}
	if v.Search != nil {
		out.Searches = []*pb.SearchState{v.Search}
	}
	return out
}

func sortedByKey[T any](m map[string]T) []T {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	out := make([]T, 0, len(keys))
	for _, k := range keys {
		out = append(out, m[k])
	}
	return out
}
