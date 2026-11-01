package core_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func TestPendingMoveIsTheLastPromotionWhenTheBranchIsNotAtIt(t *testing.T) {
	if _, pending := core.PendingMove(baseCommit, nil); pending {
		t.Error("nothing was promoted, so no move is pending")
	}
	if _, pending := core.PendingMove(candidateCommit, promoted()); pending {
		t.Error("the branch is at the last promotion, so no move is pending")
	}
	to, pending := core.PendingMove(baseCommit, promoted())
	if !pending || to != candidateCommit {
		t.Errorf("PendingMove = %s, %v; want a move to %s", to, pending, candidateCommit)
	}
}
