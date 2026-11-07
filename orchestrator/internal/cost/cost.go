// Package cost works out what a run has cost, from the finished turns of its agents. It is pure: it
// reads no clock and does no I/O, so a view is a function of the turns it was given and the instant
// it is asked for, and the same turns in any order give the same view.
//
// The host owns every number a screen shows of a run's cost. A Ledger takes the finished turns one
// at a time and answers with a View: the total and its tokens by kind, the split by type of agent
// and by model, the spend over time in buckets whose width the ledger picks, and the spend of the
// last hour. A client displays a View and adds nothing up.
//
// A turn is priced when its provider reported a cost and unpriced when it reported tokens and no
// cost. An unpriced turn is counted, with its tokens, everywhere a turn is, and adds no dollar: a
// price is never made up, so a cost with unpriced turns behind it is a lower bound and the view
// says how many.
package cost

import (
	"cmp"
	"maps"
	"math"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// MaxBuckets is the most buckets a series holds.
const MaxBuckets = 48

// RateWindow is how far back LastHour looks.
const RateWindow = time.Hour

// ladder is the widths a series' buckets take, each a whole number of minutes so that a turn's
// bucket follows from its minute. The ladder is fixed, so the same run is cut the same way by every
// reader and a bucket's edges stay put until the run is long enough to need the next width.
var ladder = []time.Duration{
	time.Minute, 5 * time.Minute, 15 * time.Minute, 30 * time.Minute,
	time.Hour, 2 * time.Hour, 3 * time.Hour, 6 * time.Hour, 12 * time.Hour,
	24 * time.Hour, 48 * time.Hour, 7 * 24 * time.Hour,
}

// beyondLadder is the unit of the widths past the ladder: a run that long is cut in whole weeks.
const beyondLadder = 7 * 24 * time.Hour

// WidthFor is the width of the buckets of a run that has gone on for span: the narrowest rung of the
// ladder that holds the span in MaxBuckets buckets or fewer, and past the ladder the fewest whole
// weeks that do.
func WidthFor(span time.Duration) time.Duration {
	for _, w := range ladder {
		if span <= w*MaxBuckets {
			return w
		}
	}
	weeks := (span + MaxBuckets*beyondLadder - 1) / (MaxBuckets * beyondLadder)
	return weeks * beyondLadder
}

// ModelKey names a model as an agent runs it. An empty Model is the harness's own default.
type ModelKey struct {
	Harness core.Harness
	Model   core.ModelName
}

// RoleCost is what the agents of one type have cost.
type RoleCost struct {
	Role core.AgentRole
	// Agents is how many agents of the type the run has had, over or not.
	Agents uint32
	Turns  uint32
	// Unpriced counts the turns that reported tokens and no cost.
	Unpriced uint32
	// Usage is the tokens by kind and the cost of the priced turns, nil where none reported.
	Usage core.Usage
	// Tokens is TotalTokens of Usage.
	Tokens uint64
}

// ModelCost is what one model has cost.
type ModelCost struct {
	Model    ModelKey
	Turns    uint32
	Unpriced uint32
	Usage    core.Usage
	// Tokens is TotalTokens of Usage.
	Tokens uint64
}

// ModelSpend is what one model added to one bucket.
type ModelSpend struct {
	Model ModelKey
	Cost  float64
}

// Bucket is what the turns that ended in one span cost.
type Bucket struct {
	Start time.Time
	// Cost is the sum of ByModel.
	Cost float64
	// ByModel is in the order of View.Models and leaves out a model with no cost in the bucket.
	ByModel []ModelSpend
}

// Series is the cost over time: consecutive buckets of one width, from the run's start to the
// instant of the view.
type Series struct {
	Start time.Time
	Width time.Duration
	// Buckets holds at least one and at most MaxBuckets. Bucket i starts at Start plus i times Width,
	// and the last ends at the view's AsOf, so it may be shorter than the rest.
	Buckets []Bucket
}

// View is what a run has cost as of an instant.
type View struct {
	Run core.RunID
	// AsOf is the instant the view is worked out at: the given time for a run that is not over, and
	// for one that is, its end, so that the view of a finished run is always the same. It is never
	// before the run's start or the end of a turn the view counts.
	AsOf time.Time
	// Total holds the tokens by kind and the cost of all finished turns, and no cost when none is
	// priced.
	Total core.Usage
	// MaxCostUSD is the run's cost limit, zero when it has none.
	MaxCostUSD float64
	Turns      uint32
	Unpriced   uint32
	// Roles has an entry for each type of agent the run has, the most costly first. A type with no
	// priced turn follows, in the order of the names.
	Roles []RoleCost
	// Models has an entry for each model the run's agents ran on, ordered as Roles are.
	Models []ModelCost
	Series Series
	// LastHour is the cost of the turns that finished in the hour up to AsOf. Nil when no turn of the
	// run is priced.
	LastHour *float64
}

// agentInfo is what a ledger keeps of an agent: the facts that group its turns.
type agentInfo struct {
	role  core.AgentRole
	model ModelKey
}

type turnKey struct {
	agent   core.AgentID
	ordinal uint32
}

// entry is one finished turn.
type entry struct {
	key   turnKey
	end   time.Time
	agent *agentInfo
	usage core.Usage
}

func entryOrder(a, b entry) int {
	return cmp.Or(a.end.Compare(b.end), cmp.Compare(a.key.agent, b.key.agent), cmp.Compare(a.key.ordinal, b.key.ordinal))
}

// Ledger is the finished turns of one run, and the agents that ran them. The zero value is empty and
// ready. A Ledger is not safe for concurrent use.
type Ledger struct {
	agents  map[core.AgentID]*agentInfo
	entries []entry
	at      map[turnKey]int
	// sorted says entries is in entryOrder, which View needs and Add keeps true while turns arrive
	// in order.
	sorted bool
}

// Agent tells the ledger an agent exists, so that its type counts even before its first turn ends,
// and says whether the ledger had not known it. Telling it again changes nothing, whatever the order
// of the calls.
func (l *Ledger) Agent(a core.Agent) (added bool) {
	_, known := l.agents[a.ID]
	l.info(a)
	return !known
}

func (l *Ledger) info(a core.Agent) *agentInfo {
	if l.agents == nil {
		l.agents = map[core.AgentID]*agentInfo{}
	}
	info := l.agents[a.ID]
	if info == nil {
		info = &agentInfo{}
		l.agents[a.ID] = info
	}
	*info = agentInfo{role: a.Role, model: ModelKey{Harness: a.Spec.Harness, Model: a.Spec.Model}}
	return info
}

// Add counts a finished turn of agent a. A turn that is still running is not counted: the ledger
// counts what the run's own usage counts. Adding a turn again replaces what it was added as, so a
// turn repeated by a stream is counted once.
func (l *Ledger) Add(t core.Turn, a core.Agent) {
	if t.Status == core.TurnRunning {
		return
	}
	e := entry{key: turnKey{agent: t.Agent, ordinal: t.Ordinal}, end: t.EndedAt, agent: l.info(a), usage: t.Usage}
	if l.at == nil {
		l.at = map[turnKey]int{}
		l.sorted = true
	}
	if i, again := l.at[e.key]; again {
		l.entries[i] = e
		l.sorted = false
		return
	}
	if n := len(l.entries); n > 0 && entryOrder(l.entries[n-1], e) > 0 {
		l.sorted = false
	}
	l.at[e.key] = len(l.entries)
	l.entries = append(l.entries, e)
}

// tokenKinds is every kind of token a usage reports.
func tokenKinds(u core.Usage) []*uint64 {
	return []*uint64{u.InputTokens, u.CacheReadTokens, u.CacheCreationTokens, u.OutputTokens, u.ReasoningTokens}
}

// hasTokens says a turn reported any tokens.
func hasTokens(u core.Usage) bool {
	for _, n := range tokenKinds(u) {
		if n != nil && *n > 0 {
			return true
		}
	}
	return false
}

// TotalTokens is all the tokens a usage reports: the sum of its five kinds, a kind it does not report
// counting as none. The host adds them up so that no client does; the sum stops at the largest count
// rather than wrapping.
func TotalTokens(u core.Usage) uint64 {
	var sum uint64
	for _, n := range tokenKinds(u) {
		if n == nil {
			continue
		}
		if sum+*n < sum {
			return math.MaxUint64
		}
		sum += *n
	}
	return sum
}

// unpriced says a turn reported tokens and no cost.
func unpriced(u core.Usage) bool { return u.CostUSD == nil && hasTokens(u) }

// tally is what a group of turns adds up to.
type tally struct {
	turns, unpriced uint32
	usage           core.Usage
}

func (t *tally) add(u core.Usage) {
	t.turns++
	if unpriced(u) {
		t.unpriced++
	}
	t.usage = t.usage.Plus(u)
}

// byCost orders groups by what they cost, the most first, and a group with no priced turn after
// those; ties go by name.
func byCost(aCost, bCost *float64, aName, bName string) int {
	switch {
	case aCost != nil && bCost != nil:
		return cmp.Or(cmp.Compare(*bCost, *aCost), cmp.Compare(aName, bName))
	case aCost != nil:
		return -1
	case bCost != nil:
		return 1
	}
	return cmp.Compare(aName, bName)
}

func (k ModelKey) name() string { return string(k.Harness) + "/" + string(k.Model) }

// View works out the view of the run as of now. Passing the run states its start, its limit and
// whether it is over.
func (l *Ledger) View(run core.Run, now time.Time) View {
	if !l.sorted {
		slices.SortFunc(l.entries, entryOrder)
		for i, e := range l.entries {
			l.at[e.key] = i
		}
		l.sorted = true
	}
	asOf := now
	if run.Status.Final() && !run.EndedAt.IsZero() {
		asOf = run.EndedAt
	}
	if n := len(l.entries); n > 0 {
		asOf = laterOf(asOf, l.entries[n-1].end)
	}
	asOf = laterOf(asOf, run.CreatedAt)

	v := View{Run: run.ID, AsOf: asOf, MaxCostUSD: run.MaxCostUSD}
	roles := map[core.AgentRole]*tally{}
	models := map[ModelKey]*tally{}
	agents := map[core.AgentRole]uint32{}
	for _, info := range l.agents {
		agents[info.role]++
		roles[info.role] = &tally{}
		models[info.model] = &tally{}
	}
	var total tally
	for _, e := range l.entries {
		total.add(e.usage)
		roles[e.agent.role].add(e.usage)
		models[e.agent.model].add(e.usage)
	}
	v.Total, v.Turns, v.Unpriced = total.usage, total.turns, total.unpriced
	for role, t := range roles {
		v.Roles = append(v.Roles, RoleCost{Role: role, Agents: agents[role], Turns: t.turns, Unpriced: t.unpriced, Usage: t.usage, Tokens: TotalTokens(t.usage)})
	}
	slices.SortFunc(v.Roles, func(a, b RoleCost) int {
		return byCost(a.Usage.CostUSD, b.Usage.CostUSD, string(a.Role), string(b.Role))
	})
	for model, t := range models {
		v.Models = append(v.Models, ModelCost{Model: model, Turns: t.turns, Unpriced: t.unpriced, Usage: t.usage, Tokens: TotalTokens(t.usage)})
	}
	slices.SortFunc(v.Models, func(a, b ModelCost) int {
		return byCost(a.Usage.CostUSD, b.Usage.CostUSD, a.Model.name(), b.Model.name())
	})
	v.Series = l.series(v.Models, run.CreatedAt, asOf)
	if v.Total.CostUSD != nil {
		v.LastHour = l.lastHour(asOf)
	}
	return v
}

// Spend works out what the turns that ended in [from, until) cost, in total and by type of agent. It is
// the window a check-in covers: a turn that ended exactly at until is in the next window, so windows
// that follow one another count every turn once, and the spend of a span is the spend of its halves.
// A turn is counted when it ended, as View's series counts it.
func (l *Ledger) Spend(from, until time.Time) core.Spend {
	roles := map[core.AgentRole]*tally{}
	var total tally
	for _, e := range l.entries {
		if e.end.Before(from) || !e.end.Before(until) {
			continue
		}
		total.add(e.usage)
		if roles[e.agent.role] == nil {
			roles[e.agent.role] = &tally{}
		}
		roles[e.agent.role].add(e.usage)
	}
	out := core.Spend{Turns: total.turns, Unpriced: total.unpriced, Usage: total.usage}
	for _, role := range slices.Sorted(maps.Keys(roles)) {
		t := roles[role]
		out.Roles = append(out.Roles, core.RoleSpend{Role: role, Turns: t.turns, Unpriced: t.unpriced, Usage: t.usage})
	}
	return out
}

func laterOf(a, b time.Time) time.Time {
	if b.After(a) {
		return b
	}
	return a
}

// series cuts the priced turns into buckets. A turn is in the bucket its end falls in, and one that
// ended before the run's start (a clock set back) or after asOf is in the first or the last, so the
// buckets hold every turn.
func (l *Ledger) series(models []ModelCost, start, asOf time.Time) Series {
	span := asOf.Sub(start)
	width := WidthFor(span)
	n := max(1, int((span+width-1)/width))
	s := Series{Start: start, Width: width, Buckets: make([]Bucket, n)}
	spend := make([]map[ModelKey]float64, n)
	for _, e := range l.entries {
		if e.usage.CostUSD == nil {
			continue
		}
		i := min(max(int(e.end.Sub(start)/width), 0), n-1)
		if spend[i] == nil {
			spend[i] = map[ModelKey]float64{}
		}
		spend[i][e.agent.model] += *e.usage.CostUSD
	}
	for i := range s.Buckets {
		b := &s.Buckets[i]
		b.Start = start.Add(time.Duration(i) * width)
		for _, m := range models {
			if c, ok := spend[i][m.Model]; ok && c > 0 {
				b.ByModel = append(b.ByModel, ModelSpend{Model: m.Model, Cost: c})
				b.Cost += c
			}
		}
	}
	return s
}

// lastHour sums the priced turns that ended in (asOf - RateWindow, asOf]. It is called only when a
// turn is priced, so it always has a number to give.
func (l *Ledger) lastHour(asOf time.Time) *float64 {
	from := asOf.Add(-RateWindow)
	var sum float64
	for i := len(l.entries) - 1; i >= 0; i-- {
		e := l.entries[i]
		if !e.end.After(from) {
			break
		}
		if e.usage.CostUSD != nil && !e.end.After(asOf) {
			sum += *e.usage.CostUSD
		}
	}
	return &sum
}
