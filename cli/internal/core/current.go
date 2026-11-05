package core

// NoCurrentResultReason is why `result` has nothing to report.
type NoCurrentResultReason string

// The reasons.
const (
	// NoHead means nothing was promoted, so there is no head.
	NoHead NoCurrentResultReason = "no_head"
	// NotMeasuredSincePromotion means the last promotion set the head and the head has no
	// measurement and check taken after it. The cache cannot supply one: reusing the samples that
	// picked a winner would overstate its score. A fresh measure of the head gives it a number.
	NotMeasuredSincePromotion NoCurrentResultReason = "not_measured_since_promotion"
	// HeadNotPromoted means the branch was moved outside promote, to a head the last promotion did
	// not set. No measurement gives it a number: nothing records when the branch moved, so no
	// measurement can be shown to be taken after the move, and the head may have been picked on the
	// very samples that would overstate it. The next promotion sets a head that can have one.
	HeadNotPromoted NoCurrentResultReason = "head_not_promoted"
)

// ResultAnswer is what `result` reports: a CurrentResult or a NoCurrentResult.
type ResultAnswer interface {
	resultAnswer()
}

// CurrentResult is the head and a final number measured after the head last moved.
type CurrentResult struct {
	Head   Commit
	Result Result
}

// NoCurrentResult says why there is no final number, and the head when there is one.
type NoCurrentResult struct {
	Reason NoCurrentResultReason
	Head   Commit
}

func (CurrentResult) resultAnswer()   {}
func (NoCurrentResult) resultAnswer() {}

// CurrentResultAt decides what `result` reports when the result branch is at head. It is
// CurrentResultOf, except that a head the last promotion did not set has no number: the branch
// moved by some other means, so no measurement of the last promoted commit speaks for it, and the
// answer is HeadNotPromoted for the head it is at. A branch that was never promoted has no head to
// report, whatever commit it is at.
func CurrentResultAt(head Commit, promotions []Promotion, results []Result) ResultAnswer {
	if len(promotions) == 0 {
		return NoCurrentResult{Reason: NoHead}
	}
	if head != promotions[len(promotions)-1].Commit {
		return NoCurrentResult{Reason: HeadNotPromoted, Head: head}
	}
	return CurrentResultOf(promotions, results)
}

// CurrentResultOf decides what `result` reports. The head is the commit of the last promotion
// (promotions are oldest first). The answer is its latest measurement that was recorded after the
// head last moved and rests on a passing check that was also recorded after it. A measurement
// or a check from before the move never counts, so a saved result cannot satisfy this however
// recent the cache makes it look: the cache answers submissions, and this answers a request for a
// number taken fresh. Results of other commits are ignored.
func CurrentResultOf(promotions []Promotion, results []Result) ResultAnswer {
	if len(promotions) == 0 {
		return NoCurrentResult{Reason: NoHead}
	}
	last := promotions[len(promotions)-1]
	var latest *Result
	for _, r := range measurementsOf(last.Commit, results) {
		checkedAt, passed := checkPassOf(r, results)
		if passed && r.RecordedAt.After(last.At) && checkedAt.After(last.At) {
			latest = &r
		}
	}
	if latest == nil {
		return NoCurrentResult{Reason: NotMeasuredSincePromotion, Head: last.Commit}
	}
	return CurrentResult{Head: last.Commit, Result: *latest}
}
