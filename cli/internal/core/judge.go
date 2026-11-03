package core

// Judgment is how one commit's saved measurements stand against the head's: the comparison, or
// the failure that refused it. It is a pure function of the results it was made from, so a commit
// is judged again whenever the head moves or a measurement lands.
type Judgment struct {
	Commit  Commit
	Against Commit
	// Comparison is set when the commit could be compared, and then Refused is nil.
	Comparison *Comparison
	// Refused is why the commit could not be compared, such as FailureNotMeasured when a side has
	// no measurement and FailureEnvironmentMismatch when the sides were taken in two environments.
	Refused *Failure
}

// Judge compares a commit's measurements with the head's. results are saved results; those of
// other commits, and checks, are ignored.
func Judge(commit, head Commit, results []Result, relativeNoise float64) Judgment {
	out := Judgment{Commit: commit, Against: head}
	cmp, err := CompareCommits(CompareRequest{Commit: commit, Against: head, Results: results, RelativeNoise: relativeNoise})
	if err != nil {
		f := AsFailure(err)
		out.Refused = &f
		return out
	}
	out.Comparison = &cmp
	return out
}

// Verdict is the verdict of a judgment that compared, and nil for one that was refused.
func (j Judgment) Verdict() *Verdict {
	if j.Comparison == nil {
		return nil
	}
	v := j.Comparison.Verdict
	return &v
}
