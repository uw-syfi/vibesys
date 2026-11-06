package app_test

import (
	"errors"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A promotion moves the result branch, which a person may have checked out. A clean checkout moves
// with it. One with local changes makes the promotion fail as a conflict that names it, and nothing
// is recorded or moved, so the run goes on and the same promotion works once the checkout is clean.
func TestPromotingABranchThatIsCheckedOutKeepsTheCheckout(t *testing.T) {
	r := newResultsRig(t)
	r.measured(base, level)
	r.measured(cm(1), win)
	r.branches.AddCheckout("/repo", "/home/person/repo", resultBranch)

	r.promoted(cm(1))
	if at, dirty := r.branches.CheckoutState("/repo", "/home/person/repo"); at != cm(1) || dirty {
		t.Fatalf("after the promotion the checkout holds %.7s (changes: %v), want %.7s", at, dirty, cm(1))
	}

	better := make([]float64, len(win))
	for i, v := range win {
		better[i] = v * 1.4
	}
	r.measured(cm(2), better)
	r.branches.DirtyCheckout("/repo", "/home/person/repo")
	_, err := r.promote(cm(2))
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureConflict || !strings.Contains(f.Message, "/home/person/repo") {
		t.Fatalf("Promote = %v, want a conflict that names the checkout", err)
	}
	if r.head() != cm(1) {
		t.Fatalf("a refused promotion moved the branch to %.7s", r.head())
	}
	if got := len(r.recorded()); got != 1 {
		t.Fatalf("a refused promotion left %d records, want only the first", got)
	}
}
