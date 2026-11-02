package core

import "slices"

// Carrying out a promotion is two writes to two places that cannot be written together: the
// record of the promotion in the store, and the move of the result branch in the repository. They
// are done in that order, so a crash between them leaves a record the branch has not caught up
// with, and never a branch that is ahead of the record.
//
// NextPromotionStep is the whole sequence as a pure function. It looks at what is there, the
// branch head and the record, and names the one effect to do next. The caller does the effect and
// asks again, and keeps asking until the answer is PromotionDone. Because the answer depends only
// on what is there and never on what the caller did before, a crash or a failure at any point
// leaves a state from which the same question has the right next step: recovery is the same loop.

// PromotionStep is the next effect of carrying out a promotion.
type PromotionStep interface {
	promotionStep()
}

// RecordPromotionStep says to record the promotion. Nothing else has changed yet.
type RecordPromotionStep struct{ Promotion Promotion }

// MoveBranchStep says to fast-forward the result branch from the head the promotion replaced to
// the promoted commit. The promotion is already recorded.
type MoveBranchStep struct{ From, To Commit }

// PromotionDoneStep says that the promotion is recorded and the branch is at the promoted commit.
type PromotionDoneStep struct{}

// PromotionDivergedStep says that the branch is at a commit that is neither the one the promotion
// replaced nor the promoted one, so someone else moved it. Nothing may be done to it.
type PromotionDivergedStep struct{ Head Commit }

func (RecordPromotionStep) promotionStep()   {}
func (MoveBranchStep) promotionStep()        {}
func (PromotionDoneStep) promotionStep()     {}
func (PromotionDivergedStep) promotionStep() {}

// NextPromotionStep returns the next effect of carrying out a promotion, given the branch head and
// the record of promotions, oldest first.
//
//   - Not recorded and the head is where the promotion left it: record it.
//   - Recorded and the head is still where it was: move the branch.
//   - Recorded and the head is the promoted commit: done.
//   - Anything else: the branch was moved by someone else, which is diverged.
//
// Only the promotion that is being carried out, the newest, can be asked about: an older one has
// a head that later promotions moved on.
func NextPromotionStep(p Promotion, head Commit, promotions []Promotion) PromotionStep {
	recorded := slices.ContainsFunc(promotions, func(q Promotion) bool {
		return q.Commit == p.Commit && q.PreviousHead == p.PreviousHead && q.At.Equal(p.At)
	})
	switch {
	case !recorded && head == p.PreviousHead:
		return RecordPromotionStep{Promotion: p}
	case recorded && head == p.PreviousHead:
		return MoveBranchStep{From: p.PreviousHead, To: p.Commit}
	case recorded && head == p.Commit:
		return PromotionDoneStep{}
	default:
		return PromotionDivergedStep{Head: head}
	}
}
