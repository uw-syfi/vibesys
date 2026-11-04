package hostlease_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/hostlease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/leasetest"
	"github.com/uw-syfi/vibesys/cli/internal/lease/locallease"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
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

// fsType is the statfs type of the file system a path is on, as stat -f prints it on the machine.
func fsType(t *testing.T, path string) uint32 {
	t.Helper()
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		t.Fatal(err)
	}
	return uint32(st.Type)
}

// A machine reached over ssh finds its directory of leases as its own daemons do: in the first
// parent that is sticky, writable by everyone and on local disk, and none on a file system of the
// network types.
func TestTheHostFindsItsLeasesAsItsDaemonsDo(t *testing.T) {
	root := t.TempDir()
	missing, private, shared := filepath.Join(root, "missing"), filepath.Join(root, "private"), filepath.Join(root, "shared")
	for p, mode := range map[string]os.FileMode{private: 0o755, shared: os.ModeSticky | 0o777} {
		if err := os.Mkdir(p, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(p, mode); err != nil {
			t.Fatal(err)
		}
	}
	host := hostlease.Host{Shell: remote.Local{}, Parents: []string{missing, private, shared}, DirName: "vibesys-leases"}
	l, err := host.Acquire(context.Background(), "machine", core.LeaseHolder{User: "guest"})
	if err != nil {
		t.Fatal(err)
	}
	defer l.Release(context.Background())
	_, err = locallease.Dir{Path: filepath.Join(shared, "vibesys-leases")}.Acquire(context.Background(), "machine", core.LeaseHolder{User: "machine"})
	var held *lease.HeldError
	if !errors.As(err, &held) || held.Holder.User != "guest" {
		t.Fatalf("the host's daemon was not refused the lease the guest holds in %s: %v", shared, err)
	}

	host.NetworkTypes = []uint32{0x6969, fsType(t, shared)}
	_, err = host.Acquire(context.Background(), "other", core.LeaseHolder{User: "guest"})
	if err == nil || !strings.Contains(err.Error(), shared+" is on a network file system") || !strings.Contains(err.Error(), private+" is not there, or not sticky") {
		t.Fatalf("a host whose only shared parent is on a network file system gave %v", err)
	}
}

// The machine's daemons and its guests over ssh find the same directory of leases on this machine.
func TestThisMachineAndItsGuestsFindTheSameLeases(t *testing.T) {
	path, err := runtimedir.ResolveLeases(runtimedir.System())
	if err != nil {
		t.Fatal(err)
	}
	key := lease.Key("test-" + strconv.Itoa(os.Getpid()))
	t.Cleanup(func() { _ = os.Remove(filepath.Join(path, string(key)+".lock")) })
	guest := hostlease.Host{Shell: remote.Local{}, Parents: runtimedir.LeaseParents, DirName: runtimedir.LeaseDirName, NetworkTypes: runtimedir.NetworkMagics()}
	l, err := guest.Acquire(context.Background(), key, core.LeaseHolder{User: "guest"})
	if err != nil {
		t.Fatal(err)
	}
	defer l.Release(context.Background())
	_, err = locallease.Dir{Path: path}.Acquire(context.Background(), key, core.LeaseHolder{User: "machine"})
	var held *lease.HeldError
	if !errors.As(err, &held) || held.Holder.User != "guest" {
		t.Fatalf("this machine's daemon was not refused, in %s, the lease its guest holds: %v", path, err)
	}
}
