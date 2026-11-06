package production

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"math/rand/v2"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// outgoing is a Fake of a host that has stopped answering and still holds the state directory's lock:
// it keeps it for a number of looks, then lets go.
type outgoing struct {
	holder *pb.HostLock
	looks  int
	opened int
}

func (h *outgoing) open() (string, error) {
	h.opened++
	if h.looks < h.opened {
		return "served", nil
	}
	return "", &HeldError{Dir: "/state", Holder: h.holder}
}

// instantly is a clock on which every wait is over at once, and that counts them.
func instantly(waits *int) func(time.Duration) <-chan time.Time {
	return func(time.Duration) <-chan time.Time {
		*waits++
		c := make(chan time.Time, 1)
		c <- time.Time{}
		return c
	}
}

// For any holder and however long it keeps the lock: a holder of this root or one that does not say
// is waited for up to the bound, by looking at the lock; one of another root is refused at the first
// look. There is exactly one outcome, and it is the host serving or the typed refusal.
func TestAStartWaitsOnTheLockOfAnOutgoingHostOnly(t *testing.T) {
	const root = "/root/of/this/host"
	rng := rand.New(rand.NewPCG(3, 4))
	holders := []struct {
		name   string
		holder *pb.HostLock
		waits  bool
	}{
		{"this root", &pb.HostLock{Pid: 7, Release: "1.0.0", Root: root}, true},
		{"an older host that does not say", &pb.HostLock{Pid: 7, Release: "0.9.0"}, true},
		{"a record not written yet", nil, true},
		{"another root", &pb.HostLock{Pid: 7, Release: "1.0.0", Root: "/another/root"}, false},
	}
	for range 400 {
		for _, c := range holders {
			looks := rng.IntN(handOverLooks + 20)
			held := &outgoing{holder: c.holder, looks: looks}
			waits := 0
			value, err := AwaitHandOver(context.Background(), root, instantly(&waits), held.open)
			switch {
			case !c.waits:
				if looks > 0 && (!errors.Is(err, ErrHeld) || waits != 0 || held.opened != 1) {
					t.Fatalf("%s for %d looks: %q, %v after %d waits, want a refusal at once", c.name, looks, value, err, waits)
				}
			case looks <= handOverLooks:
				if err != nil || value != "served" || waits != looks {
					t.Fatalf("%s for %d looks: %q, %v after %d waits, want served", c.name, looks, value, err, waits)
				}
			default:
				if !errors.Is(err, ErrHeld) || waits != handOverLooks {
					t.Fatalf("%s for %d looks: %q, %v after %d waits, want a refusal at the bound", c.name, looks, value, err, waits)
				}
			}
		}
	}
}

// A holder is judged afresh at every look: another root's host that takes the lock during the wait
// ends it.
func TestAStartStopsWaitingWhenAnotherRootTakesTheLock(t *testing.T) {
	const root = "/root"
	opened := 0
	waits := 0
	_, err := AwaitHandOver(context.Background(), root, instantly(&waits), func() (string, error) {
		opened++
		if opened < 4 {
			return "", &HeldError{Holder: &pb.HostLock{Pid: 7, Release: "1.0.0", Root: root}}
		}
		return "", &HeldError{Holder: &pb.HostLock{Pid: 8, Release: "1.0.0", Root: "/another"}}
	})
	if !errors.Is(err, ErrHeld) || opened != 4 || waits != 3 {
		t.Fatalf("%v after %d looks and %d waits", err, opened, waits)
	}
}

// Any other failure is not a hand-over, and a start that was called off ends with the refusal.
func TestAStartDoesNotWaitOnAnythingElse(t *testing.T) {
	waits := 0
	failure := errors.New("disk on fire")
	if _, err := AwaitHandOver(context.Background(), "/root", instantly(&waits), func() (string, error) { return "", failure }); !errors.Is(err, failure) || waits != 0 {
		t.Fatalf("%v after %d waits", err, waits)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	never := func(time.Duration) <-chan time.Time { return nil }
	held := &outgoing{holder: &pb.HostLock{Pid: 7, Release: "1.0.0", Root: "/root"}, looks: 1 << 30}
	if _, err := AwaitHandOver(ctx, "/root", never, held.open); !errors.Is(err, ErrHeld) || held.opened != 1 {
		t.Fatalf("%v after %d looks", err, held.opened)
	}
}

// With the real lock: the new host's open is refused while the old host holds the state directory and
// succeeds once it lets go, with the old host letting go only after several looks.
func TestAStartTakesTheRealLockWhenTheHostBeforeItLetsGo(t *testing.T) {
	const root = "/root/of/this/host"
	layout, err := LayoutFor(t.TempDir(), "/unused")
	if err != nil {
		t.Skipf("the test directory is not a local filesystem: %v", err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	wiring := func(version string) Wiring {
		return Wiring{Sidecars: NoSidecars{}, Parts: readyParts{}, Tree: fakeTree{}, Version: version, Root: root}
	}
	before, err := Open(layout, wiring("1.0.0"), log)
	if err != nil {
		t.Fatal(err)
	}
	waits := 0
	letGoAfter := func(d time.Duration) <-chan time.Time {
		if waits == 3 {
			if err := before.Close(); err != nil {
				t.Fatal(err)
			}
		}
		return instantly(&waits)(d)
	}
	next, err := AwaitHandOver(context.Background(), root, letGoAfter, func() (*Daemon, error) { return Open(layout, wiring("1.0.1"), log) })
	if err != nil {
		t.Fatalf("after %d waits: %v", waits, err)
	}
	defer next.Close()
	if waits != 4 {
		t.Fatalf("the lock was taken after %d waits, want 4", waits)
	}
}
