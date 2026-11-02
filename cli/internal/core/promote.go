package core

import (
	"slices"
	"time"
)

// PromoteRefusalKind is one reason a promotion was refused.
type PromoteRefusalKind string

// The refusal kinds.
const (
	RefusalUnmeasured          PromoteRefusalKind = "unmeasured"
	RefusalCheckMissing        PromoteRefusalKind = "check_missing"
	RefusalWorse               PromoteRefusalKind = "worse"
	RefusalWithinNoise         PromoteRefusalKind = "within_noise"
	RefusalInconclusive        PromoteRefusalKind = "inconclusive"
	RefusalStale               PromoteRefusalKind = "stale"
	RefusalEnvironmentMismatch PromoteRefusalKind = "environment_mismatch"
)

// PromoteRefusalReason is one reason with the evidence it rests on.
type PromoteRefusalReason struct {
	Kind    PromoteRefusalKind
	Message string
	// Verdict is set for worse, within_noise and inconclusive.
	Verdict *Verdict
	// MeasuredAgainst and CurrentHead are set for stale.
	MeasuredAgainst Commit
	CurrentHead     Commit
}

// PromoteRefusal is a normal answer to a promotion, not an error. It can carry several reasons.
type PromoteRefusal struct {
	Reasons []PromoteRefusalReason
}

// Promotion is a move of the result branch that happened.
type Promotion struct {
	Commit Commit
	// PreviousHead is the head the move replaced: the commit the result branch was at. For the
	// first promotion that is the commit the run started from.
	PreviousHead Commit
	Verdict      Verdict
	Environment  Fingerprint
	// ResultJob is the measurement the promotion rests on.
	ResultJob JobID
	At        time.Time
}

// PromotionDecision is the answer to a request to promote: a Promotion that may happen or a
// PromoteRefusal.
type PromotionDecision interface {
	promotionDecision()
}

func (Promotion) promotionDecision()      {}
func (PromoteRefusal) promotionDecision() {}

// PromotionRequest asks whether a commit may become the result branch's head.
type PromotionRequest struct {
	Candidate Commit
	// Head is the commit to beat: the result branch's head, or the commit the run started from
	// while nothing has been promoted.
	Head Commit
	// Results are saved results. Those of other commits are ignored.
	Results []Result
	// Promotions is the record of promotions, oldest first.
	Promotions []Promotion
	// RelativeNoise is the task's declared relative noise, zero when it declares none.
	RelativeNoise float64
	// At is when the promotion would happen.
	At time.Time
}

// DecidePromotion decides whether the candidate may be promoted. It may be only if the candidate
// is better than the head, on measurements that stand: every reason it may not be is returned
// together, in the order of the PromoteRefusalKind constants.
//
//   - RefusalUnmeasured: the candidate, or the head it is to beat, has no measurement. Nothing
//     else is judged then.
//   - RefusalCheckMissing: a measurement of the candidate does not rest on a passing check of
//     the same commit, task version and environment: it names none, or names a job that is not
//     such a pass. A check that ran inside the measuring job names that job.
//   - RefusalWorse, RefusalWithinNoise and RefusalInconclusive: the verdict of CompareCommits is
//     not better. Task version and metric mismatches are inconclusive.
//   - RefusalStale: the result branch moved after the candidate was measured, at or after the
//     time of its oldest measurement, so the measurement was taken against an older head.
//     Measure again, without the cache, after the move.
//   - RefusalEnvironmentMismatch: the measurements were not all taken in one environment.
//     Promotion refuses to mix environments, and gives no verdict then.
//
// It fails with a *Failure, rather than refusing, when the two sides cannot be compared at all:
// see Compare. The promotion it returns has the environment both sides were measured in, the
// candidate's latest measurement as the one it rests on, and the head before the move, which is
// the commit a stale refusal says the measurement was taken against.
func DecidePromotion(req PromotionRequest) (PromotionDecision, error) {
	if req.At.IsZero() {
		return nil, failf(FailureInvalidInput, "promotion: no time")
	}
	candidate := measurementsOf(req.Candidate, req.Results)
	base := measurementsOf(req.Head, req.Results)
	var reasons []PromoteRefusalReason
	if len(candidate) == 0 {
		reasons = append(reasons, PromoteRefusalReason{
			Kind:    RefusalUnmeasured,
			Message: "commit " + shortCommit(req.Candidate) + " has no measurement",
		})
	}
	if len(base) == 0 {
		reasons = append(reasons, PromoteRefusalReason{
			Kind:    RefusalUnmeasured,
			Message: "the commit to beat, " + shortCommit(req.Head) + ", has no measurement to compare with",
		})
	}
	if len(reasons) > 0 {
		return PromoteRefusal{Reasons: reasons}, nil
	}

	compared, err := compareMeasurements(candidate, base, req.RelativeNoise)
	if err != nil {
		return nil, err
	}
	if slices.ContainsFunc(candidate, func(r Result) bool { return !restsOnPassingCheck(r, req.Results) }) {
		reasons = append(reasons, PromoteRefusalReason{
			Kind:    RefusalCheckMissing,
			Message: "a measurement of commit " + shortCommit(req.Candidate) + " does not rest on a passing check of the same commit, task version and environment",
		})
	}
	if compared.mixedEnvironments == "" {
		if reason, refused := verdictRefusal(compared.verdict); refused {
			reasons = append(reasons, reason)
		}
	}
	if moved, measuredAgainst := movedSince(candidate[0].RecordedAt, req.Promotions); moved {
		reasons = append(reasons, PromoteRefusalReason{
			Kind:            RefusalStale,
			Message:         "the result branch moved after commit " + shortCommit(req.Candidate) + " was measured; measure it again with --no-cache",
			MeasuredAgainst: measuredAgainst,
			CurrentHead:     req.Head,
		})
	}
	if compared.mixedEnvironments != "" {
		reasons = append(reasons, PromoteRefusalReason{Kind: RefusalEnvironmentMismatch, Message: compared.mixedEnvironments})
	}
	if len(reasons) > 0 {
		return PromoteRefusal{Reasons: reasons}, nil
	}

	return Promotion{
		Commit:       req.Candidate,
		PreviousHead: req.Head,
		Verdict:      compared.verdict,
		Environment:  compared.environment,
		ResultJob:    candidate[len(candidate)-1].Job,
		At:           req.At,
	}, nil
}

// verdictRefusal is the refusal for a verdict that is not better.
func verdictRefusal(verdict Verdict) (PromoteRefusalReason, bool) {
	kinds := map[VerdictKind]PromoteRefusalKind{
		Worse:        RefusalWorse,
		WithinNoise:  RefusalWithinNoise,
		Inconclusive: RefusalInconclusive,
	}
	kind, refused := kinds[verdict.Kind]
	if !refused {
		return PromoteRefusalReason{}, false
	}
	var text string
	switch verdict.Kind {
	case Worse:
		text = "the candidate is worse than the commit to beat"
	case WithinNoise:
		text = "the candidate is within noise of the commit to beat"
	case Inconclusive:
		text = "the samples cannot say whether the candidate beats the commit to beat (" + string(verdict.Reason) + ")"
	}
	return PromoteRefusalReason{Kind: kind, Message: text, Verdict: &verdict}, true
}

// movedSince reports whether a promotion happened at or after a measurement was recorded, and the
// head that promotion replaced: the head the measurement was taken against. Promotions are
// oldest first.
func movedSince(measuredAt time.Time, promotions []Promotion) (bool, Commit) {
	for _, p := range promotions {
		if !p.At.Before(measuredAt) {
			return true, p.PreviousHead
		}
	}
	return false, ""
}

// restsOnPassingCheck reports whether a measurement names a passing check of its own commit,
// task version and environment. The measuring job names itself when its check ran inside it, and
// otherwise names the check job whose pass it linked.
func restsOnPassingCheck(measurement Result, results []Result) bool {
	_, ok := checkPassOf(measurement, results)
	return ok
}

// checkPassOf returns when the check pass a measurement rests on was recorded.
func checkPassOf(measurement Result, results []Result) (time.Time, bool) {
	named := measurement.Outcome.(MeasureOutcome).CheckJob
	if named == "" {
		return time.Time{}, false
	}
	if named == measurement.Job {
		return measurement.RecordedAt, true
	}
	for _, r := range results {
		if _, isCheck := r.Outcome.(CheckOutcome); isCheck && r.Job == named && r.Commit == measurement.Commit &&
			r.TaskVersion == measurement.TaskVersion && r.Environment.Equal(measurement.Environment) {
			return r.RecordedAt, true
		}
	}
	return time.Time{}, false
}
