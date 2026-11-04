package production

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/hostlease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/locallease"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// test-isolation: leasingOf is the wiring from the operator config to where the daemon leases what
// its jobs hold alone, which RunDaemon reaches only with a real runtime directory and index.

// The machine a config's one placement runs its containers on is leased where every daemon that
// uses it looks: this machine's directory of leases, or the same directory on the engine's machine
// over ssh. Slurm, and a config of several placements, lease nothing.
func TestTheLeaseIsWhereThePlacementRuns(t *testing.T) {
	p := Process{UID: 1000}
	place := func(text string) (lease.Place, bool) {
		t.Helper()
		return p.leasingOf(parse(t, text)).Book.Place(string(schedule.DefaultPlacement))
	}
	if got, ok := place(""); !ok || got.Leases != (locallease.Dir{Path: locallease.DefaultDir}) || got.Key != "machine" {
		t.Fatalf("the default config leases %+v, %v", got, ok)
	}
	got, ok := place("[placements.gpu]\nkind = \"ssh\"\n[placements.gpu.remote]\nhost = \"host-b\"\nremote_workspace_root = \"/w\"\n")
	host, isHost := got.Leases.(hostlease.Host)
	if !ok || !isHost || host.Dir != locallease.DefaultDir || host.Shell.(remote.SSH).Host != "host-b" {
		t.Fatalf("an ssh placement leases %+v, %v", got, ok)
	}
	if got, ok := place("[placements.a100]\nkind = \"slurm\"\n[placements.a100.remote]\nhost = \"login\"\nremote_workspace_root = \"/w\"\n[placements.a100.slurm]\npartition = \"gpu\"\n"); ok {
		t.Fatalf("a slurm placement leases %+v", got)
	}
	if got, ok := place("[placements.a]\nkind = \"local\"\n[placements.b]\nkind = \"local\"\n"); ok {
		t.Fatalf("two placements lease %+v", got)
	}
	if h := p.leasingOf(parse(t, "")).Holder; h.PID <= 0 || h.User == "" {
		t.Fatalf("the daemon is named %+v", h)
	}
}
