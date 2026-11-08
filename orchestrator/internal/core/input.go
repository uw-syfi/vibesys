package core

import "time"

// Input is one thing that happened, with everything Decide must not find out for itself.
type Input struct {
	// At is when it happened.
	At time.Time
	// Nonce is unique to this input. Decide builds every new id from it, and a replay reads those
	// ids from the events, so it is never needed twice.
	Nonce Nonce
	// Models is the host's model policy as it stands, which a start or a spawn is held to.
	Models ModelPolicy
	Body   InputBody
}

// InputBody is what happened. The set is closed.
type InputBody interface{ inputBody() }

// Caller is who a command comes from. The shell sets it from the connection or the agent's
// session, never from anything the caller sent. The set is closed, and a nil Caller is refused, so
// a forgotten caller does not act as the operator.
type Caller interface{ caller() }

// The callers.
type (
	// Operator is the person at a client. There is one operator principal, whoever is at the client:
	// what the operator may do never depends on Viewer, which is only the label the client chose for
	// its install, so that a check-in can say whose it is. Nothing authenticates it, and it is no
	// part of a command's digest.
	Operator struct{ Viewer Viewer }
	// AgentCaller is an agent, through its session.
	AgentCaller struct{ Agent AgentID }
)

func (Operator) caller()    {}
func (AgentCaller) caller() {}

// Command is a request from a caller.
type Command struct {
	ID CommandID
	// Digest identifies the command's content, so that one id cannot name two commands.
	Digest Digest
	Caller Caller
	Body   CommandBody
}

func (Command) inputBody() {}

// CommandBody is what a command asks for. The set is closed.
type CommandBody interface{ action() Action }

// The commands.
type (
	// StartRun starts a run and launches its lead.
	StartRun struct {
		Repository  string
		Task        string
		FanoutLimit uint32
		// MaxTime is how long the run may take from its start, zero for no time limit.
		MaxTime time.Duration
		// MaxCostUSD is the run's cost limit in dollars, zero for none.
		MaxCostUSD float64
		// SummaryEvery is how often the lead is asked for a progress report while the run's work moves,
		// zero for DefaultSummaryEvery. The shell's validation holds it to the contract's least.
		SummaryEvery time.Duration
		Lead         AgentSpec
		Brief        string
		// Allow adds to what the run may use. The lead's own harness and model are always allowed.
		Allow []AgentChoice
		// EvalPlace is the eval place chosen, empty to let the host resolve it. The shell resolves it
		// before the command is decided and sets the run's place; it is part of what the client sent.
		EvalPlace string
		// JobTool is what the run's agents use. The shell sets it, as the tool answered it before the
		// command was decided: it is no part of what the client sent, or of the command's digest.
		JobTool JobToolEndpoint
	}
	// StopRun stops a run and every agent in it.
	StopRun struct{ Run RunID }
	// SpawnAgent launches a subagent. An empty harness takes the lead's. An empty model takes the
	// first model the run allows on that harness. An empty effort takes the lead's on the lead's
	// harness, and is left to the harness's default on another. A harness and model the run does
	// not allow are refused.
	SpawnAgent struct {
		Run        RunID
		Role       AgentRole
		Spec       AgentSpec
		PlanNumber uint32
		Brief      string
	}
	// StopAgent stops one subagent.
	StopAgent struct{ Agent AgentID }
	// SendMessage sends text to an agent.
	SendMessage struct {
		Agent AgentID
		Text  string
	}
	// PostHandoff sends the lead the caller's result. The caller is done: once its turn ends and
	// no mail waits, its session closes.
	PostHandoff struct{ Handoff Handoff }
	// PostReport sends the operator a report.
	PostReport struct{ Report Report }
	// RequestCatchUp records a check-in for the operator's viewer: everything in the run since that
	// viewer's last one. See checkin.go.
	RequestCatchUp struct{ Run RunID }
)

func (StartRun) action() Action       { return ActionStartRun }
func (StopRun) action() Action        { return ActionStopRun }
func (SpawnAgent) action() Action     { return ActionSpawnAgent }
func (StopAgent) action() Action      { return ActionStopAgent }
func (SendMessage) action() Action    { return ActionSendMessage }
func (PostHandoff) action() Action    { return ActionPostHandoff }
func (PostReport) action() Action     { return ActionPostReport }
func (RequestCatchUp) action() Action { return ActionRequestCatchUp }

// Session names one session of one agent.
type Session struct {
	Agent AgentID
	N     uint32
}

// SessionFact is one event from a session's sidecar. Deltas never get here: the shell passes them
// straight to whoever is watching, so a fact is always something to record.
type SessionFact struct {
	Session Session
	// Offset increases within a session. A fact at or below what is committed is a repeat.
	Offset uint64
	Body   FactBody
}

func (SessionFact) inputBody() {}

// FactBody is what a sidecar reported. The set is closed.
type FactBody interface{ factBody() }

// The facts.
type (
	// SessionOpened says the session is ready.
	SessionOpened struct {
		// ResumeToken is what a later session opens with to continue this one. Opaque here.
		ResumeToken  string
		Capabilities Capabilities
	}
	// ItemObserved is an item, new or changed. Ordinal, Agent and At are filled in here.
	ItemObserved struct {
		ID     ItemID
		Turn   TurnID
		Status ItemStatus
		Body   ItemBody
	}
	// TurnEnded says a turn is over, with a final status.
	TurnEnded struct {
		Turn   TurnID
		Status TurnStatus
		Usage  Usage
	}
	// SteerOutcome answers one steer. A steer too late for its turn comes after that TurnEnded.
	SteerOutcome struct {
		Message  MessageID
		Accepted bool
	}
	// UsageReported is usage so far in a turn.
	UsageReported struct {
		Turn  TurnID
		Usage Usage
	}
	// SessionFailed says the session cannot go on.
	SessionFailed struct {
		Detail string
		// StartFailure is set when the session failed before it opened. It is ignored for one that
		// had opened.
		StartFailure StartFailure
	}
	// SessionClosed confirms the session is over.
	SessionClosed struct{}
)

func (SessionOpened) factBody() {}
func (ItemObserved) factBody()  {}
func (TurnEnded) factBody()     {}
func (SteerOutcome) factBody()  {}
func (UsageReported) factBody() {}
func (SessionFailed) factBody() {}
func (SessionClosed) factBody() {}

// SessionLost says what holds a session is gone: the shell could not reach it, could not start it,
// could not work with it, or found it missing after a restart. It is the one fact about a session
// that session cannot send.
type SessionLost struct {
	Session Session
	// Detail says why, such as the error that stopped a launch. It reaches the agent's record and,
	// for a subagent, its lead.
	Detail string
	// StartFailure says why a session that was never opened could not be, when the shell can tell.
	// Empty is read as StartOther for a session that was launching, and ignored for one that was not.
	StartFailure StartFailure
}

func (SessionLost) inputBody() {}

// JobUpdate is a job's state as vibesys-cli reports it. A feed may repeat one.
type JobUpdate struct {
	Job         JobID
	Run         RunID
	State       JobState
	SubmittedBy AgentID
	Detail      string
}

func (JobUpdate) inputBody() {}

// JobsRunning is how many of a run's jobs the job tool reports as queued or running. The shell sends
// it after every poll that read the jobs, so it is level-triggered: a repeat changes nothing.
type JobsRunning struct {
	Run   RunID
	Count uint32
}

func (JobsRunning) inputBody() {}

// CheckoutsReleased says a ReleaseCheckouts was carried out, and which working copies it kept. The
// answer to a run that is not over, or one already recorded, changes nothing.
type CheckoutsReleased struct {
	Run  RunID
	Kept []Checkout
}

func (CheckoutsReleased) inputBody() {}

// WindowFactsReady answers a ComputeWindowFacts with the facts of the check-in's window. They are
// frozen in the check-in: an answer for a check-in that is no longer pending, such as a repeat, changes
// nothing.
type WindowFactsReady struct {
	CheckIn CheckInID
	Facts   WindowFacts
}

func (WindowFactsReady) inputBody() {}

// WindowFactsFailed answers a ComputeWindowFacts when the facts cannot be worked out because a record
// they are made of cannot be read now. Detail says what, in words for the viewer. The check-in fails,
// and the viewer's next one starts where this one would have.
type WindowFactsFailed struct {
	CheckIn CheckInID
	Detail  string
}

func (WindowFactsFailed) inputBody() {}

// TimerFired says a timer an ArmTimer asked for is due. A timer can outlive what it was armed for,
// and then it does nothing.
type TimerFired struct{ Timer ArmTimer }

func (TimerFired) inputBody() {}
