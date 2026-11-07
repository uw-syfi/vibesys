package cost

import (
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// MaxHostRuns is the most runs a HostView lists. The runs past it are added up in Others, so the view
// stays as short as the contract promises however many runs the host holds.
const MaxHostRuns = 32

// RunCost is what one run has cost, as a line of the host's list of runs by cost.
type RunCost struct {
	Run core.RunID
	// Name is what the run is called: its task.
	Name   string
	Status core.RunStatus
	// MaxCostUSD is the run's cost limit, zero when it has none.
	MaxCostUSD float64
	// Turns are the finished turns the host counts of the run, and Unpriced those of them that reported
	// tokens and no cost.
	Turns    uint32
	Unpriced uint32
	// Usage is the tokens by kind and the cost of the priced turns, no cost when none is priced.
	Usage core.Usage
	// Tokens is TotalTokens of Usage.
	Tokens uint64
}

// OtherRuns is what the runs that did not make HostView.Runs have cost, added up.
type OtherRuns struct {
	// Runs is how many runs are added up; zero when every run is listed.
	Runs     uint32
	Turns    uint32
	Unpriced uint32
	Usage    core.Usage
	// Tokens is TotalTokens of Usage.
	Tokens uint64
}

// HostView is what the runs of a host have cost as of an instant: the Breakdown of every counted
// run's turns together, and the runs one by one.
type HostView struct {
	// AsOf is the instant the view is worked out at: the given time while a counted run is live, and
	// once none is, the latest end among the counted runs, so that the view of a host with nothing live
	// is always the same. It is never before the first bucket's start or the end of a turn the view
	// counts.
	AsOf time.Time
	// RunCount is how many runs the view counts: len(Runs) plus Others.Runs.
	RunCount uint32
	Breakdown
	// Runs lists the most costly runs, the most first, at most MaxHostRuns. A run with no priced turn
	// follows the others, in the order of the run ids.
	Runs   []RunCost
	Others OtherRuns
}

// Host is the finished turns of the runs a host holds and the facts of those runs, which it folds
// into a HostView. It works out nothing a run's Ledger does not: its Breakdown is the Breakdown of one
// ledger that holds every counted run's turns, so the splits, the series and the rate are the code a
// run's view runs, and a counted run's line is the total of that run's own turns as its own view
// works it out.
//
// A Host counts the runs it is told of (Run) except one that ended before its window began, the
// agents of those runs, and their turns that ended in the window. It is not safe for concurrent use.
type Host struct {
	since  time.Time
	runs   map[core.RunID]core.Run
	ledger Ledger
}

// NewHost starts a host that counts the turns that ended at or after since, and the runs that were
// not over before it. The zero time is every turn of every run.
func NewHost(since time.Time) *Host {
	return &Host{since: since, runs: map[core.RunID]core.Run{}}
}

// Run tells the host a run exists, or what became of it. It says whether the host counts the run, and
// whether the view it shows of it changed: the run is new, or its status or its end moved. A run that
// ended before the window is not counted, and its agents and turns are left out.
func (h *Host) Run(r core.Run) (counted, changed bool) {
	if r.Status.Final() && r.EndedAt.Before(h.since) {
		return false, false
	}
	prev, known := h.runs[r.ID]
	h.runs[r.ID] = r
	return true, !known || prev.Status != r.Status || !prev.EndedAt.Equal(r.EndedAt)
}

// Agent tells the host an agent exists, so that its type counts before its first turn ends, and says
// whether the host had not known it. An agent of a run the host does not count is left out.
func (h *Host) Agent(a core.Agent) (added bool) {
	if _, counted := h.runs[a.Run]; !counted {
		return false
	}
	return h.ledger.Agent(a)
}

// Add counts a finished turn of agent a, as Ledger.Add does, when it ended in the host's window and
// the agent's run is counted. A turn that is still running is not counted.
func (h *Host) Add(t core.Turn, a core.Agent) {
	if _, counted := h.runs[a.Run]; !counted || t.EndedAt.Before(h.since) {
		return
	}
	h.ledger.Add(t, a)
}

// Live says a counted run is not over, so that the view moves with the clock.
func (h *Host) Live() bool {
	for _, r := range h.runs {
		if !r.Status.Final() {
			return true
		}
	}
	return false
}

// View works out the view of the counted runs as of now.
func (h *Host) View(now time.Time) HostView {
	h.ledger.order()
	start, asOf := h.span(now)
	v := HostView{AsOf: asOf, RunCount: uint32(len(h.runs)), Breakdown: h.ledger.breakdown(start, asOf)}
	rows := h.lines()
	if len(rows) > MaxHostRuns {
		for _, r := range rows[MaxHostRuns:] {
			v.Others.add(r)
		}
		rows = rows[:MaxHostRuns]
	}
	v.Runs = rows
	return v
}

func (o *OtherRuns) add(r RunCost) {
	o.Runs++
	o.Turns += r.Turns
	o.Unpriced += r.Unpriced
	o.Usage = o.Usage.Plus(r.Usage)
	o.Tokens = TotalTokens(o.Usage)
}

// span is where the series starts and the instant the view is as of. The window's start is the
// series' start, and with none it is the start of the first counted run.
func (h *Host) span(now time.Time) (start, asOf time.Time) {
	start, asOf = h.since, now
	var ended time.Time
	for _, r := range h.runs {
		if h.since.IsZero() && (start.IsZero() || r.CreatedAt.Before(start)) {
			start = r.CreatedAt
		}
		ended = laterOf(ended, laterOf(r.EndedAt, r.CreatedAt))
	}
	if len(h.runs) > 0 && !h.Live() {
		asOf = ended
	}
	if end, ok := h.ledger.lastEnd(); ok {
		asOf = laterOf(asOf, end)
	}
	if start.IsZero() {
		start = asOf
	}
	return start, laterOf(asOf, start)
}

// lines is one line for each counted run, in the order HostView.Runs states: the total of the run's
// turns in the ledger, which are the turns of the run's own view in the order that view adds them.
func (h *Host) lines() []RunCost {
	tallies := make(map[core.RunID]*tally, len(h.runs))
	for id := range h.runs {
		tallies[id] = &tally{}
	}
	for _, e := range h.ledger.entries {
		tallies[e.agent.run].add(e.usage)
	}
	rows := make([]RunCost, 0, len(h.runs))
	for id, r := range h.runs {
		t := tallies[id]
		rows = append(rows, RunCost{
			Run: id, Name: r.Task, Status: r.Status, MaxCostUSD: r.MaxCostUSD,
			Turns: t.turns, Unpriced: t.unpriced, Usage: t.usage, Tokens: TotalTokens(t.usage),
		})
	}
	slices.SortFunc(rows, func(a, b RunCost) int {
		return byCost(a.Usage.CostUSD, b.Usage.CostUSD, string(a.Run), string(b.Run))
	})
	return rows
}
