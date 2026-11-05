package core

// NoCurrentResultReason is why `result` has nothing to report.
type NoCurrentResultReason string

// The reasons.
const (
	// NoHead means nothing was promoted, so there is no head.
	NoHead NoCurrentResultReason = "no_head"
	// HeadNotMeasured means the head has no saved measurement that rests on a passing check of
	// the same commit, task version and environment. A measure of the head gives it one.
	HeadNotMeasured NoCurrentResultReason = "head_not_measured"
)

// ResultAnswer is what `result` reports: a CurrentResult or a NoCurrentResult.
type ResultAnswer interface {
	resultAnswer()
}

// CurrentResult is the head and a final number: a measurement of the head commit itself.
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

// CurrentResultAt decides what `result` reports when the result branch is at head, and head is
// empty when nothing was promoted. The answer is the head's latest measurement that rests on a
// passing check of the same commit, task version and environment (the check a measurement ran in
// its own job, or the saved pass it linked), whenever either was taken and whatever moved the
// branch to the head: a promotion, or a commit such as a task switch made on the branch. Results
// of other commits are ignored, so nothing measured in another task version or for another commit
// stands for the head: a commit's task version is that of the task its tree holds, so the head's
// own measurements are all of the head's task version. A failed check or measure saves no result,
// so it never answers.
//
// The measurements a promotion rests on are measurements of the head, and they count: a second,
// fresh measure of a commit already measured in its environment would spend a whole measure on a
// number already known.
func CurrentResultAt(head Commit, results []Result) ResultAnswer {
	if head == "" {
		return NoCurrentResult{Reason: NoHead}
	}
	var latest *Result
	for _, r := range measurementsOf(head, results) {
		if restsOnPassingCheck(r, results) {
			latest = &r
		}
	}
	if latest == nil {
		return NoCurrentResult{Reason: HeadNotMeasured, Head: head}
	}
	return CurrentResult{Head: head, Result: *latest}
}
