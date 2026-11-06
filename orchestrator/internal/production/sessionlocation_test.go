package production

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// A root so long that a session's socket cannot fit in a Unix socket address is refused with a typed
// reason, before any process starts, and a root that fits is not. The reason is what the client turns
// into advice; the text is only for people.
func TestASessionRootTooLongForItsSocketIsRefusedWithATypedReason(t *testing.T) {
	long := "/" + strings.Repeat("d", maxSocketPath)
	w := newHolderWorld()
	_, err := w.sidecars(long).Launch(context.Background(), sessionStart("agent-1", core.RoleLead, "/repo"))
	var failed *daemon.LaunchError
	if !errors.As(err, &failed) || failed.Reason != core.StartStateLocationTooLong {
		t.Fatalf("got %v, want a launch error with reason %q", err, core.StartStateLocationTooLong)
	}
	if w.spawns != 0 {
		t.Fatalf("%d processes started for a session that could not be reached", w.spawns)
	}

	// The boundary is the socket path itself: exactly the limit is accepted.
	if _, err := w.sidecars("/sessions").Launch(context.Background(), sessionStart("agent-1", core.RoleLead, "/repo")); err != nil {
		t.Fatalf("a short root was refused: %v", err)
	}
}
