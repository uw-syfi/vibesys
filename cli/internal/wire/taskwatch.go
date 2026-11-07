package wire

import (
	"buf.build/go/protovalidate"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// JudgedCommits is how many commits a WatchTask judges for what the request asked. It is checked
// at the wire boundary by protovalidate: 0 means the default, and more than the most a
// watch may judge is INVALID_INPUT.
func JudgedCommits(r *cliv1.WatchTaskRequest) (int, error) {
	if err := protovalidate.Validate(r); err != nil {
		return 0, &core.Failure{Kind: core.FailureInvalidInput, Message: err.Error()}
	}
	return core.ResolveJudgedCommits(r.GetJudgedCommits())
}

var awayReasons = map[core.AwayReason]cliv1.AwayReason{
	core.AwayOwnedElsewhere: cliv1.AwayReason_AWAY_REASON_OWNED_ELSEWHERE,
	core.AwayUnavailable:    cliv1.AwayReason_AWAY_REASON_UNAVAILABLE,
}

// StoreHealth returns the contract's message for the health of a task's store.
func StoreHealth(h core.StoreHealth) *cliv1.StoreHealth {
	if h.Away == "" {
		return &cliv1.StoreHealth{State: &cliv1.StoreHealth_Live{Live: &cliv1.Live{}}}
	}
	return &cliv1.StoreHealth{State: &cliv1.StoreHealth_Away{Away: &cliv1.Away{Reason: awayReasons[h.Away], Failure: Failure(h.Failure)}}}
}

// Head returns the contract's message for the head of the result branch and its number.
func Head(a core.ResultAnswer) *cliv1.Head {
	switch a := a.(type) {
	case core.CurrentResult:
		return &cliv1.Head{State: &cliv1.Head_Current{Current: &cliv1.CurrentResult{Head: string(a.Head), Result: Result(a.Result)}}}
	case core.NoCurrentResult:
		return &cliv1.Head{State: &cliv1.Head_None{None: &cliv1.NoCurrentResult{Reason: noResultReasons[a.Reason], Head: string(a.Head)}}}
	}
	return &cliv1.Head{}
}

// Judgment returns the contract's message for a judgment.
func Judgment(j core.Judgment) *cliv1.Judgment {
	out := &cliv1.Judgment{Commit: string(j.Commit), Against: string(j.Against)}
	switch {
	case j.Comparison != nil:
		out.Outcome = &cliv1.Judgment_Comparison{Comparison: Comparison(*j.Comparison)}
	case j.Refused != nil:
		out.Outcome = &cliv1.Judgment_Refused{Refused: Failure(*j.Refused)}
	}
	return out
}

// TaskFact returns the contract's message for a fact, and false for a value that is not one.
func TaskFact(f core.Fact) (*cliv1.TaskFact, bool) {
	out := &cliv1.TaskFact{}
	switch v := f.Value.(type) {
	case core.Job:
		out.Fact = &cliv1.TaskFact_Job{Job: Job(v)}
	case core.Result:
		out.Fact = &cliv1.TaskFact_Result{Result: Result(v)}
	case core.Judgment:
		out.Fact = &cliv1.TaskFact_Judgment{Judgment: Judgment(v)}
	case core.PromotionRecord:
		out.Fact = &cliv1.TaskFact_Promotion{Promotion: &cliv1.PromotionRecord{Seq: v.Seq, Promotion: Promotion(v.Promotion)}}
	case core.Plan:
		out.Fact = &cliv1.TaskFact_Plan{Plan: Plan(v)}
	case core.Bound:
		out.Fact = &cliv1.TaskFact_Bound{Bound: Bound(v)}
	case core.CurrentResult, core.NoCurrentResult:
		out.Fact = &cliv1.TaskFact_Head{Head: Head(v.(core.ResultAnswer))}
	case core.StoreHealth:
		out.Fact = &cliv1.TaskFact_Health{Health: StoreHealth(v)}
	case core.JobTrouble:
		out.Fact = &cliv1.TaskFact_Trouble{Trouble: &cliv1.JobTrouble{JobId: string(v.Job), Failure: Failure(v.Failure)}}
	default:
		return nil, false
	}
	return out, true
}

// TaskMessages returns the messages that say a Send, without an epoch or a sequence, which the
// stream puts on. A reset is a Reset, the whole state in messages of at most
// core.MaxFactsPerMessage facts, and a CaughtUp. A change is one message of facts.
func TaskMessages(s core.Send) []*cliv1.WatchTaskResponse {
	var out []*cliv1.WatchTaskResponse
	if s.Reset {
		out = append(out, &cliv1.WatchTaskResponse{Body: &cliv1.WatchTaskResponse_Reset_{Reset_: &cliv1.Reset{}}})
	}
	for rest := s.Facts; len(rest) > 0; {
		n := min(len(rest), core.MaxFactsPerMessage)
		facts := &cliv1.TaskFacts{}
		for _, f := range rest[:n] {
			if m, ok := TaskFact(f); ok {
				facts.Facts = append(facts.Facts, m)
			}
		}
		if len(facts.Facts) > 0 {
			out = append(out, &cliv1.WatchTaskResponse{Body: &cliv1.WatchTaskResponse_Facts{Facts: facts}})
		}
		rest = rest[n:]
	}
	if s.Reset {
		out = append(out, &cliv1.WatchTaskResponse{Body: &cliv1.WatchTaskResponse_CaughtUp{CaughtUp: &cliv1.CaughtUp{}}})
	}
	return out
}
