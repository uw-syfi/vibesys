package core

import (
	"cmp"
	"fmt"
	"reflect"
	"slices"
	"strconv"
	"strings"
)

// This file is the decision behind WatchTask: what a task's state is as a set of keyed facts, and,
// given the facts a stream last sent, what it sends next. Both are pure. The daemon reads a task's
// store into a TaskView on every wake and asks PlanSend what to say; nothing here knows of a
// store, a wake or a wire format.

const (
	// DefaultJudgedCommits is how many of the newest measured commits a watch judges when it does
	// not say.
	DefaultJudgedCommits = 20
	// MaxJudgedCommits is the most a watch may ask to judge.
	MaxJudgedCommits = 100
	// MaxFactsPerMessage is the most facts one message carries. A change of more facts than this
	// is sent as a reset, so no message is larger than this and no change needs two.
	MaxFactsPerMessage = 500
)

// ResolveJudgedCommits is the number of commits a watch judges for what it asked: 0 asks for the
// default, and more than MaxJudgedCommits is invalid input.
func ResolveJudgedCommits(asked uint32) (int, error) {
	switch {
	case asked == 0:
		return DefaultJudgedCommits, nil
	case asked > MaxJudgedCommits:
		return 0, &Failure{Kind: FailureInvalidInput, Message: fmt.Sprintf("judged_commits: %d is more than the most a watch may judge, %d", asked, MaxJudgedCommits)}
	default:
		return int(asked), nil
	}
}

// AwayReason is why a task's store could not be read.
type AwayReason string

// The reasons.
const (
	// AwayOwnedElsewhere means another daemon or machine owns the store.
	AwayOwnedElsewhere AwayReason = "owned_elsewhere"
	// AwayUnavailable means the store did not answer or could not be read for another reason.
	AwayUnavailable AwayReason = "unavailable"
)

// StoreHealth says whether the task's store was read.
type StoreHealth struct {
	// Away is empty when the store was read.
	Away AwayReason
	// Failure is why, when Away is set.
	Failure Failure
}

// PromotionRecord is a promotion with its place in the task's record, 1 for the first.
type PromotionRecord struct {
	Seq       uint64
	Promotion Promotion
}

// JobTrouble is why a job that is not final is not being worked on.
type JobTrouble struct {
	Job     JobID
	Failure Failure
}

// TaskContent is everything the store holds about one task that a watch reports. Every slice is in
// the order of its fact kind's key.
type TaskContent struct {
	Head       ResultAnswer
	Plans      []Plan
	Bounds     []Bound
	Promotions []PromotionRecord
	Jobs       []Job
	Results    []Result
	Judgments  []Judgment
	Troubles   []JobTrouble
}

// TaskView is what one look at a task found. Content is nil when the store could not be read,
// and then the health says why; a stream that sent content before keeps it.
type TaskView struct {
	Health  StoreHealth
	Content *TaskContent
}

// FactKey names one fact: the kind, then what identifies it within the kind. A fact replaces the
// fact with the same key.
type FactKey string

// Fact is one keyed piece of a task's state. Value is a Job, Result, Judgment, PromotionRecord,
// Plan, Bound, ResultAnswer (the head), StoreHealth or JobTrouble.
type Fact struct {
	Key   FactKey
	Value any
}

const (
	keyHealth     = FactKey("health")
	keyHead       = FactKey("head")
	prefixJudged  = "judgment/"
	prefixTrouble = "trouble/"
)

// Facts returns the view's facts in the order they are sent: health, head, plans, bounds,
// promotions, jobs, results, judgments, troubles, each by its key. A view with no content has only its
// health.
func (v TaskView) Facts() []Fact {
	out := []Fact{{Key: keyHealth, Value: v.Health}}
	c := v.Content
	if c == nil {
		return out
	}
	out = append(out, Fact{Key: keyHead, Value: c.Head})
	for _, p := range c.Plans {
		out = append(out, Fact{Key: FactKey("plan/" + strconv.Itoa(int(p.Number))), Value: p})
	}
	for _, b := range c.Bounds {
		out = append(out, Fact{Key: FactKey("bound/" + strconv.FormatUint(b.Seq, 10)), Value: b})
	}
	for _, p := range c.Promotions {
		out = append(out, Fact{Key: FactKey("promotion/" + strconv.FormatUint(p.Seq, 10)), Value: p})
	}
	for _, j := range c.Jobs {
		out = append(out, Fact{Key: FactKey("job/" + string(j.ID)), Value: j})
	}
	for _, r := range c.Results {
		out = append(out, Fact{Key: FactKey("result/" + string(r.Job)), Value: r})
	}
	for _, j := range c.Judgments {
		out = append(out, Fact{Key: FactKey(prefixJudged + string(j.Commit)), Value: j})
	}
	for _, t := range c.Troubles {
		out = append(out, Fact{Key: FactKey(prefixTrouble + string(t.Job)), Value: t})
	}
	return out
}

// Sent is what a stream last sent, by key.
type Sent map[FactKey]any

// Send is what a stream sends next. When Reset is set, Facts is the whole state and a client
// replaces what it has with it; otherwise Facts are the changed facts, at most MaxFactsPerMessage,
// each replacing the fact with its key. A Send with no facts and no reset says nothing.
type Send struct {
	Reset bool
	Facts []Fact
}

// Empty says there is nothing to send.
func (s Send) Empty() bool { return !s.Reset && len(s.Facts) == 0 }

// PlanSend decides what to send for a view given what was sent, and returns the state the stream
// holds once it has sent it.
//
// The first send, a key that stopped existing, and a change of more than MaxFactsPerMessage facts
// are a reset: a client cannot be told a fact is gone, and one message cannot hold the change. A
// judgment that is no longer among the judged commits is the exception. It carries the head it
// was made against, so a client sees it is stale, and it is dropped from the stream's state
// without a reset, which keeps a head move from resetting every client. A view whose store could
// not be read changes only the health: what was sent stays true until it is read again.
func PlanSend(sent Sent, v TaskView) (Send, Sent) {
	facts := v.Facts()
	if v.Content == nil && sent != nil {
		held := Sent{}
		for k, val := range sent {
			held[k] = val
		}
		var changed []Fact
		if old, had := sent[keyHealth]; !had || !reflect.DeepEqual(old, v.Health) {
			changed = append(changed, facts[0])
		}
		held[keyHealth] = v.Health
		return Send{Facts: changed}, held
	}
	next := make(Sent, len(facts))
	var changed []Fact
	for _, f := range facts {
		next[f.Key] = f.Value
		if old, had := sent[f.Key]; !had || !reflect.DeepEqual(old, f.Value) {
			changed = append(changed, f)
		}
	}
	reset := sent == nil || len(changed) > MaxFactsPerMessage
	for k := range sent {
		if _, still := next[k]; !still && !isJudgment(k) {
			reset = true
		}
	}
	if reset {
		return Send{Reset: true, Facts: facts}, next
	}
	return Send{Facts: changed}, next
}

func isJudgment(k FactKey) bool { return strings.HasPrefix(string(k), prefixJudged) }

// NewestMeasured returns the commits of the newest n measurements, newest first, each once. The
// results are in any order.
func NewestMeasured(results []Result, n int) []Commit {
	measured := make([]Result, 0, len(results))
	for _, r := range results {
		if _, ok := r.Outcome.(MeasureOutcome); ok {
			measured = append(measured, r)
		}
	}
	slices.SortStableFunc(measured, func(a, b Result) int {
		switch {
		case a.RecordedAt.After(b.RecordedAt):
			return -1
		case a.RecordedAt.Before(b.RecordedAt):
			return 1
		default:
			return -cmp.Compare(string(a.Job), string(b.Job))
		}
	})
	var out []Commit
	for _, r := range measured {
		if len(out) == n {
			break
		}
		if !slices.Contains(out, r.Commit) {
			out = append(out, r.Commit)
		}
	}
	return out
}
