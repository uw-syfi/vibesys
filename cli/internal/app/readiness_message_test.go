package app

import (
	"context"
	"errors"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// test-isolation: malformed internal readiness messages cannot be constructed
// through the public App API; verify the loop's input boundary rejects them.
func TestReadinessPostsRequireAGeneration(t *testing.T) {
	for _, readiness := range []storeReadiness{storeReadable, storeRecoveryPending} {
		a := &App{ctx: context.Background(), inbox: make(chan message, 1)}
		err := a.post(message{readiness: readiness})
		var failure *core.Failure
		if !errors.As(err, &failure) || failure.Kind != core.FailureInternal {
			t.Fatalf("post without a generation = %v; want a typed internal failure", err)
		}
	}
}
