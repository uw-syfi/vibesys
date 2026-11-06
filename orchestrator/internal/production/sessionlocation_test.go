package production

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/privatesocket"
)

// A root so long that a session's socket cannot fit in a Unix socket address is refused with a typed
// reason, before any process starts, and a root that fits is not. The reason is what the client turns
// into advice; the text is only for people.
func TestASessionRootTooLongForItsSocketIsRefusedWithATypedReason(t *testing.T) {
	long := "/" + strings.Repeat("d", privatesocket.MaxPath)
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

// A session root of any length gets a socket that fits, when the host may place it: the place is the
// same on every call, so a restarted host finds the holder, and two sessions never share one.
func TestASessionRootOfAnyLengthGetsASocketThatFits(t *testing.T) {
	for _, length := range []int{1, 30, 80, 100, 150, 400} {
		w := newHolderWorld()
		sidecars := w.sidecars("/" + strings.Repeat("d", length))
		sidecars.Place = privatesocket.Place
		var sockets []string
		for _, agent := range []core.AgentID{"agent-1", "agent-2"} {
			session := core.Session{Agent: agent, N: 1}
			if _, err := sidecars.Launch(context.Background(), sessionStart(string(agent), core.RoleLead, "/repo")); err != nil {
				t.Fatalf("a root of %d bytes: %v", length, err)
			}
			place := sidecars.place(session)
			if !privatesocket.Fits(place.Socket) || place != sidecars.place(session) {
				t.Fatalf("a root of %d bytes: the socket %s does not fit or changes", length, place.Socket)
			}
			sockets = append(sockets, place.Socket)
		}
		if sockets[0] == sockets[1] {
			t.Fatalf("a root of %d bytes: two sessions share %s", length, sockets[0])
		}
	}
}
