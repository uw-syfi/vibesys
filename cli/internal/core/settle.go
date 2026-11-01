package core

// PendingMove says whether the last promotion has not been applied to the result branch, and the
// commit the branch is to be moved to.
//
// `promote` records a promotion and then moves the branch, in that order, so a crash between the
// two leaves a promotion the branch has not caught up with. The branch is behind it when it is at
// some other commit than the promoted one: the caller then asks git whether the head is an
// ancestor of the promoted commit, and only then moves it. A branch that is not behind, because it
// is ahead or elsewhere, is not touched, since moving it is not a fast-forward.
func PendingMove(head Commit, promotions []Promotion) (Commit, bool) {
	if len(promotions) == 0 {
		return "", false
	}
	last := promotions[len(promotions)-1].Commit
	return last, head != last
}
