package core

import "time"

// Run is one optimization of one task.
type Run struct {
	ID          RunID
	Repository  string
	Task        string
	Status      RunStatus
	Lead        AgentID
	FanoutLimit uint32
	// Deadline is the zero time when the run has none.
	Deadline   time.Time
	CreatedAt  time.Time
	StopReason RunStopReason
	EndedAt    time.Time
	// Location is where the run's agents find the vibesys-cli and its daemon and store. It is
	// recorded when the run starts and every session of the run is launched with it.
	Location CliLocation
}

// CliLocation names the vibesys-cli an agent runs and the places that build reads its daemon and
// store from. Each is an absolute path, or empty for "not set", which the agent's environment
// leaves unset.
type CliLocation struct {
	CLI        string
	RuntimeDir string
	StateHome  string
	Home       string
}

// AgentSpec says what an agent runs on.
type AgentSpec struct {
	Harness Harness
	Model   ModelName
	Effort  EffortName
}

// Capabilities are what a session can do, as its sidecar reports them. All are false until the
// session opens, which is the safe answer: nothing is steered into or resumed from a session that
// has not said it can.
type Capabilities struct {
	StreamsText bool
	Steers      bool
	Resumes     bool
}

// Agent is one session we launched, in a tree under the lead.
type Agent struct {
	ID             AgentID
	Run            RunID
	Parent         AgentID
	Role           AgentRole
	Spec           AgentSpec
	PlanNumber     uint32
	Worktree       string
	Status         AgentStatus
	ConversationID string
	Capabilities   Capabilities
	StartedAt      time.Time
	EndedAt        time.Time
	// Session counts the agent's sessions and fences them: a fact from an earlier one is dropped.
	Session uint32
	Stop    StopStage
	// HandedOff says a handoff was posted and no turn has started since.
	HandedOff bool
	// SessionFailures counts sessions in a row lost or failed without completing a turn.
	SessionFailures uint32
	// EndDetail says why the current session ended when it was lost or failed, such as the launch
	// error. It is empty while the session is live and when the end had no reason to give.
	EndDetail string
	// StartFailure says why the session never opened, when it was lost or failed while launching.
	// It is empty for a session that opened and while one is live.
	StartFailure StartFailure
}

// Usage is what a turn has cost. Fields a provider does not report are nil, never zero.
type Usage struct {
	InputTokens         *uint64
	CacheReadTokens     *uint64
	CacheCreationTokens *uint64
	OutputTokens        *uint64
	ReasoningTokens     *uint64
	CostUSD             *float64
}

// merged returns u with every field that newer reports replaced.
func (u Usage) merged(newer Usage) Usage {
	pick := func(old, next *uint64) *uint64 {
		if next != nil {
			return next
		}
		return old
	}
	out := Usage{
		InputTokens:         pick(u.InputTokens, newer.InputTokens),
		CacheReadTokens:     pick(u.CacheReadTokens, newer.CacheReadTokens),
		CacheCreationTokens: pick(u.CacheCreationTokens, newer.CacheCreationTokens),
		OutputTokens:        pick(u.OutputTokens, newer.OutputTokens),
		ReasoningTokens:     pick(u.ReasoningTokens, newer.ReasoningTokens),
		CostUSD:             u.CostUSD,
	}
	if newer.CostUSD != nil {
		out.CostUSD = newer.CostUSD
	}
	return out
}

// Turn is one unit of work by an agent, started by one message.
type Turn struct {
	ID        TurnID
	Agent     AgentID
	Ordinal   uint32
	Trigger   TurnTrigger
	Message   MessageID
	Status    TurnStatus
	Usage     Usage
	StartedAt time.Time
	EndedAt   time.Time
}

// ItemBody is what an item holds. The set is closed.
type ItemBody interface{ itemBody() }

// The item bodies.
type (
	// Assistant is text the agent wrote.
	Assistant struct{ Text string }
	// Reasoning is reasoning the provider showed.
	Reasoning struct{ Text string }
	// Prompt is text the agent was given.
	Prompt struct{ Text string }
	// ToolCall is one tool use, which changes in place as it runs.
	ToolCall struct {
		Tool           string
		ProviderCallID string
		ArgsJSON       string
		Output         string
		// ExitCode is nil when the tool has no exit code.
		ExitCode *int32
		IsError  bool
	}
)

func (Assistant) itemBody() {}
func (Reasoning) itemBody() {}
func (Prompt) itemBody()    {}
func (ToolCall) itemBody()  {}

// Item is one ordered entry in a turn.
type Item struct {
	ID      ItemID
	Turn    TurnID
	Agent   AgentID
	Ordinal uint32
	Status  ItemStatus
	At      time.Time
	Body    ItemBody
}

// MessageBody is what a message holds. The set is closed.
type MessageBody interface{ messageBody() }

// The message bodies.
type (
	// Text is free text from the operator or another agent.
	Text struct{ Text string }
	// Brief is a subagent's first prompt, as data a template renders.
	Brief struct{ Text string }
	// LeadBrief is a run's lead's first prompt, as data a template renders: the task, how many
	// subagents may be live at once, and the operator's steering text, empty when there is none.
	LeadBrief struct {
		Task        string
		FanoutLimit uint32
		Steering    string
	}
	// Handoff is what a finished agent tells the lead.
	Handoff struct {
		PlanNumber   uint32
		HasCandidate bool
		Claim        string
		Commit       Commit
		ParentCommit Commit
		Branch       string
		Checks       string
		Measurement  string
		Learned      string
		Next         string
		OpenItems    string
	}
	// Report is what the lead tells the operator.
	Report struct {
		Headline    string
		Changed     string
		InFlight    string
		Decisions   string
		OpenForUser string
	}
	// JobFinished tells the lead a job is over.
	JobFinished struct {
		Job         JobID
		Outcome     JobState
		SubmittedBy AgentID
		Detail      string
	}
	// SubagentSettled tells the lead a subagent stopped working without a handoff.
	SubagentSettled struct {
		Agent  AgentID
		Status AgentStatus
		Detail string
	}
)

func (Text) messageBody()            {}
func (Brief) messageBody()           {}
func (LeadBrief) messageBody()       {}
func (Handoff) messageBody()         {}
func (Report) messageBody()          {}
func (JobFinished) messageBody()     {}
func (SubagentSettled) messageBody() {}

// Message is typed mail between agents and the operator.
type Message struct {
	ID MessageID
	// Run is the run the message belongs to.
	Run RunID
	// From is empty for the operator or the orchestrator.
	From AgentID
	// To is empty for the operator.
	To        AgentID
	Delivery  Delivery
	At        time.Time
	Rejection Rejection
	// Attempts counts the turns started to deliver the message.
	Attempts uint32
	Body     MessageBody
}

// trigger says what kind of turn the message starts.
func (m Message) trigger() TurnTrigger {
	switch m.Body.(type) {
	case Brief, LeadBrief:
		return TriggerBrief
	case Handoff:
		return TriggerHandoff
	case JobFinished:
		return TriggerJobFinished
	case SubagentSettled:
		return TriggerSubagentSettled
	}
	if m.From == "" {
		return TriggerOperator
	}
	return TriggerMessage
}

// Receipt is the answer to a command, and the same answer to each retry of it.
type Receipt struct {
	Command CommandID
	Digest  Digest
	// Refusal is empty when the command was accepted.
	Refusal  Refusal
	Run      RunID
	Agent    AgentID
	Message  MessageID
	Delivery Delivery
}
