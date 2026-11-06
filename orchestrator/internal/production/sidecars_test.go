package production

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// A process id the system has given to another program after the holder exited must never be taken
// for the holder, or ended as one. This test process stands for that other program: it is alive, and
// nothing listens on the session's socket, which is the only proof of life there is.
func TestAnotherProgramWithTheHoldersOldProcessIDIsNotTheHolder(t *testing.T) {
	root := t.TempDir()
	log := slog.New(slog.DiscardHandler)
	sidecars := &ProcessSidecars{Root: root, Holders: &ProcessHolders{Log: log}, Log: log}
	session := core.Session{Agent: "agent-1", N: 1}
	place := sidecars.place(session)
	if err := os.MkdirAll(place.Directory, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(place.Directory, holderPidFile), fmt.Appendf(nil, "%d\n", os.Getpid()), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := sidecars.Attach(context.Background(), session); !errors.Is(err, daemon.ErrSidecarGone) {
		t.Fatalf("attaching to a pid that is another program gave %v, want ErrSidecarGone", err)
	}
	if err := sidecars.Holders.End(place); err != nil {
		t.Fatal(err)
	}
}
