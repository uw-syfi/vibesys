package production

import (
	"context"
	"errors"
	"log/slog"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// A process id the system has given to another program after the holder exited must never be taken
// for the holder, or ended as one. The record names this test process, which stands for that other
// program: it is alive, and the holder's lock is free, which is the only proof of life there is.
func TestAnotherProgramWithTheHoldersOldProcessIDIsNotTheHolder(t *testing.T) {
	log := slog.New(slog.DiscardHandler)
	signals := &recordingSignals{}
	sidecars := &ProcessSidecars{Root: t.TempDir(), Holders: &ProcessHolders{Log: log, Signals: signals}, Log: log}
	session := core.Session{Agent: "agent-1", N: 1}
	place := sidecars.place(session)
	holder := startFakeProcess(t, place, 4242, 4243)
	holder.die()

	if _, err := sidecars.Attach(context.Background(), session); !errors.Is(err, daemon.ErrSidecarGone) {
		t.Fatalf("attaching to a pid that is another program gave %v, want ErrSidecarGone", err)
	}
	if err := sidecars.Holders.End(place); err != nil {
		t.Fatal(err)
	}
	if got := signals.holders(); len(got) != 0 {
		t.Fatalf("signalled %v, which no holder held a lock for", got)
	}
	if got := signals.groups(); len(got) != 1 || got[0] != 4243 {
		t.Fatalf("ended groups %v, want only the dead holder's agent, 4243", got)
	}
}
