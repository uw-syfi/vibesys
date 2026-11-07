package core

import "time"

// This file holds the facts about a stretch of a run, which a check-in freezes when it is made. They
// are plain data: the core stores them and never works them out, because what they are made of (the
// job log of the task, the finished turns of the run) is not in a State. The package window works
// them out from the host's records, and WindowFactsReady brings them back as an input, so that a
// replay reads them from the event and never derives them a second time.
//
// A window is the half-open span [Since, Until): a record dated exactly Until belongs to the next
// window, so consecutive windows share no record and leave none out. Every figure below is a function
// of the records dated inside the window, except Objectives, which says where each objective stood
// at the two ends.
//
// A price that no turn reported is absent, never zero, wherever a Usage appears: a window in which no
// turn ended has no usage at all, and one whose turns reported tokens and no cost says so in Unpriced.

// Direction says which way an objective improves.
type Direction string

// The directions.
const (
	Maximize Direction = "maximize"
	Minimize Direction = "minimize"
)

// Spend is what the turns that ended inside a window cost.
type Spend struct {
	// Turns counts the finished turns, and Unpriced those of them that reported tokens and no cost.
	Turns, Unpriced uint32
	// Usage is the tokens by kind and the cost of the priced turns, and has no cost when none is priced.
	Usage Usage
	// Roles has an entry for each type of agent that ended a turn in the window, in the order of the
	// role names.
	Roles []RoleSpend
}

// RoleSpend is what the turns of one type of agent cost in a window.
type RoleSpend struct {
	Role            AgentRole
	Turns, Unpriced uint32
	Usage           Usage
}

// Best is the best measurement of an objective up to a time, and the candidate that made it.
type Best struct {
	Value float64
	// Job is the measure job and Commit the candidate it measured.
	Job    JobID
	Commit Commit
	// At is when the measurement was recorded.
	At time.Time
}

// ObjectiveMove is where one objective stood at the start of a window and at its end, among the
// measurements the run made. An end that is absent means nothing was measured by then, and a start
// that is absent means nothing was measured before the window. An objective nothing measured by the
// end of the window is not listed.
type ObjectiveMove struct {
	Metric    string
	Direction Direction
	// Unit is what the values are counted in, "" when unknown (see search.CommonUnit).
	Unit  string
	Start *Best
	End   *Best
}

// WindowFacts is what happened in a run during a window.
type WindowFacts struct {
	Spend Spend
	// AgentsStarted and AgentsEnded count the agents whose start and end fall in the window. An agent
	// that was resumed after it ended has no end to count, because the record keeps the current session.
	AgentsStarted, AgentsEnded uint32
	// JobsMeasured counts the measurements the run's agents recorded in the window.
	JobsMeasured uint32
	// Objectives is in the order of the metric names.
	Objectives []ObjectiveMove
	// PlansCreated and PlansChanged are the numbers of the plans made, and of the older plans edited, in
	// the window, ascending. Plans belong to a repository and its task, and a repository has one run at
	// a time, so what changed while a run was live is the run's.
	PlansCreated, PlansChanged []uint32
	// Reports are the reports the lead posted in the window, in the order posted.
	Reports []MessageID
}
