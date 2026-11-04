package hostlease_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/hostlease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/leasetest"
	"github.com/uw-syfi/vibesys/cli/internal/lease/locallease"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// world reaches this machine through the local shell, which runs the same script ssh would run on
// another: every daemon's lease is a shell of its own, as each daemon's connection would be.
func world(t leasetest.TB) leasetest.World {
	h := hostlease.Host{Shell: remote.Local{}, Dir: filepath.Join(t.TempDir(), "leases")}
	kill := func(l lease.Lease) { l.(*hostlease.Lease).Kill() }
	return leasetest.World{
		Daemon: func() lease.Leases { return h },
		Crash:  kill,
		Lose:   kill,
	}
}

func TestTheLeasesOfAHostKeepTheContract(t *testing.T) {
	leasetest.Run(t, leasetest.Contract{New: world, Seeds: 5})
}

// TestTheMachineAndItsGuestsShareLeases has a daemon of the machine and one that reaches it over a
// shell take turns at one lease, as two users' daemons would.
func TestTheMachineAndItsGuestsShareLeases(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "leases")
	local := locallease.Dir{Path: dir}
	guest := hostlease.Host{Shell: remote.Local{}, Dir: dir}
	ctx := context.Background()
	l, err := guest.Acquire(ctx, "machine", core.LeaseHolder{User: "guest", PID: 7})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := local.Acquire(ctx, "machine", core.LeaseHolder{User: "local"}); err == nil {
		t.Fatalf("the machine's daemon took the lease a guest holds")
	} else if held, ok := err.(*lease.HeldError); !ok || held.Holder.User != "guest" || held.Holder.PID != 7 {
		t.Fatalf("the machine's daemon was told %v, want the guest named", err)
	}
	if err := l.Release(ctx); err != nil {
		t.Fatal(err)
	}
	m, err := local.Acquire(ctx, "machine", core.LeaseHolder{User: "local", PID: 8})
	if err != nil {
		t.Fatalf("the lease was not given back: %v", err)
	}
	defer m.Release(ctx)
	if _, err := guest.Acquire(ctx, "machine", core.LeaseHolder{User: "guest"}); err == nil {
		t.Fatalf("a guest took the lease the machine's daemon holds")
	} else if held, ok := err.(*lease.HeldError); !ok || held.Holder.User != "local" || held.Holder.PID != 8 {
		t.Fatalf("the guest was told %v, want the machine's daemon named", err)
	}
}

func TestALinkIsNeverWrittenThrough(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "leases")
	if err := os.MkdirAll(dir, 0o777); err != nil {
		t.Fatal(err)
	}
	victim := filepath.Join(t.TempDir(), "victim")
	if err := os.WriteFile(victim, []byte("keep me"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(victim, filepath.Join(dir, "machine.lock")); err != nil {
		t.Fatal(err)
	}
	h := hostlease.Host{Shell: remote.Local{}, Dir: dir}
	if l, err := h.Acquire(context.Background(), "machine", core.LeaseHolder{User: "mallory"}); err == nil {
		l.Release(context.Background())
		t.Fatalf("a lease was taken through a link")
	}
	if b, _ := os.ReadFile(victim); string(b) != "keep me" {
		t.Fatalf("the file a link pointed at was written: %q", b)
	}
}
