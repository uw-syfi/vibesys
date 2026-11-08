package production

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/hostlease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/kubelease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/locallease"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// test-isolation: leasingOf is the wiring from the operator config to where the daemon leases what
// its jobs hold alone, which RunDaemon reaches only with a real runtime directory and index.

// leasesDir is this machine's directory of leases, as RunDaemon resolves it.
const leasesDir = "/run/lock/vibesys-leases"

// shared is the scope of a production daemon on a machine whose leases are in leasesDir.
var shared = leaseScope{local: leasesDir, remoteName: "vibesys-leases"}

// configs are operator configs of every placement kind, alone and together, with and without the
// resources a placement gives.
var configs = []string{
	"",
	"[placements.gpu]\nkind = \"ssh\"\n[placements.gpu.remote]\nhost = \"host-b\"\nremote_workspace_root = \"/w\"\n",
	"[placements.a100]\nkind = \"slurm\"\n[placements.a100.remote]\nhost = \"login\"\nremote_workspace_root = \"/w\"\n[placements.a100.slurm]\npartition = \"gpu\"\n",
	"[placements.a]\nkind = \"local\"\n[placements.b]\nkind = \"local\"\n",
	"[placements.a]\nkind = \"local\"\n[placements.a.resources.gpu]\ndevices = \"all\"\n[placements.a.resources.kubernetes]\nkubeconfig = \"/k\"\n" +
		"[placements.b]\nkind = \"ssh\"\n[placements.b.remote]\nhost = \"host-b\"\nremote_workspace_root = \"/w\"\n[placements.b.resources.kubernetes]\nkubeconfig = \"/k\"\n",
	"[placements.mi210]\nkind = \"apptainer\"\n[placements.mi210.remote]\nhost = \"login\"\nremote_workspace_root = \"/w\"\n[placements.mi210.slurm]\npartition = \"gpu\"\n" +
		"[placements.mi210.resources.rocm]\n",
	"[placements.mi300]\nkind = \"apptainer\"\n[placements.mi300.remote]\nhost = \"login\"\nremote_workspace_root = \"/w\"\n[placements.mi300.slurm]\npartition = \"gpu\"\n" +
		"[placements.mi300.apptainer]\nlocal_scratch_dir = \"/scratch/me\"\nconvert_timeout_seconds = 5400\n",
}

// claimsOf are what the resources of each placement claim, as machineEnvironments has them: the
// GPUs and the cluster of the placement's machine, which every job holds alone.
func claimsOf(t *testing.T, text string) map[string][]core.Claim {
	t.Helper()
	out := map[string][]core.Claim{}
	for name, pl := range parse(t, text).GetPlacements() {
		where := "local"
		if r := pl.GetRemote(); r != nil {
			where = r.GetHost()
		}
		if pl.GetResources().GetGpu() != nil {
			out[name] = append(out[name], core.Claim{Resource: string(core.ResourceGPU) + ":" + where + "/all", Amount: 1})
		}
		if pl.GetResources().GetRocm() != nil {
			gpus := where
			if pl.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER {
				// Each job gets the GPUs of an allocation of its own: they are the placement's.
				gpus = name + "@" + where
			}
			out[name] = append(out[name], core.Claim{Resource: string(core.ResourceROCm) + ":" + gpus, Amount: 1})
		}
		if k := pl.GetResources().GetKubernetes(); k != nil {
			out[name] = append(out[name], core.Claim{Resource: string(core.ResourceKubernetes) + ":" + where + k.GetKubeconfig(), Amount: 1, Exclusive: true})
		}
	}
	return out
}

// Every resource a measure holds alone, as the scheduler of the config has it, is leased where
// every daemon that uses it looks (a measure of an apptainer placement holds a node of its own in
// Slurm instead, so nothing alone here), but the GPUs, which the lease of their machine covers, and what
// a Slurm placement gives, which Slurm arbitrates: a placement's slots as the machine its engine
// runs on, a host job's as this machine, and a cluster in the cluster.
func TestEveryResourceAMeasureHoldsAloneIsLeasedWhereItIs(t *testing.T) {
	p := Process{UID: 1000}
	for _, text := range configs {
		op, claims := parse(t, text), claimsOf(t, text)
		s, err := schedulerOf(op, nil, claims)
		if err != nil {
			t.Fatal(err)
		}
		book := p.leasingOf(op, shared, claims, clock.System{}, nil).Book
		measure := func(env core.EnvironmentKind, placement string) []schedule.Claim {
			options := s.Demand.Options(core.Job{Spec: core.JobSpec{Environment: env, Placement: placement, Settings: core.MeasureSettings{}}})
			if len(options) != 1 {
				t.Fatalf("a measure of %s restricted to %q may run on %+v, want there alone", env, placement, options)
			}
			return options[0].Claims
		}
		jobs := map[string][]schedule.Claim{"a host job": measure(core.EnvironmentHost, "")}
		// A measure of an apptainer placement of [slurm] exclusive (the default) holds a node of
		// its own in Slurm, so it holds nothing alone here.
		ownNode := map[string]bool{}
		for name, pl := range op.GetPlacements() {
			jobs["a job of "+name] = measure(core.EnvironmentDocker, name)
			ownNode["a job of "+name] = pl.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER
		}
		for job, cs := range jobs {
			for _, c := range cs {
				if c.Exclusive == ownNode[job] {
					t.Fatalf("config %q: %s holds %s alone: %v", text, job, c.Resource, c.Exclusive)
				}
				if !c.Exclusive {
					continue
				}
				got, ok := book.Place(string(c.Resource))
				want, leased := wantPlace(t, text, string(c.Resource))
				if ok != leased || (ok && !samePlace(got, want)) {
					t.Errorf("config %q: %s holds %s alone, leased at %+v (%v), want %+v (%v)", text, job, c.Resource, got, ok, want, leased)
				}
			}
		}
	}
	if h := p.leasingOf(parse(t, ""), shared, nil, clock.System{}, nil).Holder; h.PID <= 0 || h.User == "" {
		t.Fatalf("the daemon is named %+v", h)
	}
}

// wantPlace is where a resource of a config of the table is leased, said by hand.
func wantPlace(t *testing.T, text, resource string) (lease.Place, bool) {
	t.Helper()
	here := lease.Place{Leases: locallease.Dir{Path: leasesDir}, Key: "machine"}
	hostB := lease.Place{Leases: hostlease.Host{
		Shell: remote.SSH{Host: "host-b", Program: []string{"ssh"}}, Parents: runtimedir.LeaseParents, DirName: "vibesys-leases", NetworkTypes: runtimedir.NetworkMagics(),
	}, Key: "machine"}
	switch {
	case resource == string(hostResource), strings.Contains(text, "kind = \"local\"") && strings.HasPrefix(resource, "placement:a"),
		resource == "placement:default", text == configs[3] && resource == "placement:b":
		return here, true
	case resource == "placement:gpu", text == configs[4] && resource == "placement:b":
		return hostB, true
	case resource == "kubernetes:local/k":
		return lease.Place{Key: "cluster", Leases: kubelease.Cluster{API: kubelease.Kubectl{Shell: remote.Local{}, Kubeconfig: "/k"}}}, true
	case resource == "kubernetes:host-b/k":
		return lease.Place{Key: "cluster", Leases: kubelease.Cluster{API: kubelease.Kubectl{Shell: remote.SSH{Host: "host-b", Program: []string{"ssh"}}, Kubeconfig: "/k"}}}, true
	}
	return lease.Place{}, false
}

// samePlace compares two places by what decides where the lease is: the kind of place, its machine,
// its directory or cluster, and the key.
func samePlace(a, b lease.Place) bool {
	if a.Key != b.Key {
		return false
	}
	switch x := a.Leases.(type) {
	case locallease.Dir:
		y, ok := b.Leases.(locallease.Dir)
		return ok && x == y
	case hostlease.Host:
		y, ok := b.Leases.(hostlease.Host)
		return ok && x.Dir == y.Dir && x.Shell.(remote.SSH).Host == y.Shell.(remote.SSH).Host && x.DirName == y.DirName &&
			slices.Equal(x.Parents, y.Parents) && slices.Equal(x.NetworkTypes, y.NetworkTypes)
	case kubelease.Cluster:
		y, ok := b.Leases.(kubelease.Cluster)
		kx, ky := x.API.(kubelease.Kubectl), y.API.(kubelease.Kubectl)
		return ok && x.Prefix == y.Prefix && kx.Kubeconfig == ky.Kubeconfig && shellHost(kx.Shell) == shellHost(ky.Shell)
	}
	return false
}

func shellHost(s remote.Shell) string {
	if ssh, ok := s.(remote.SSH); ok {
		return ssh.Host
	}
	return "local"
}

// A sandbox daemon keeps every lease of every placement kind where no other daemon looks: this
// machine's in its own runtime directory, an ssh machine's in a directory named for it, a
// cluster's under a prefix named for it. It never asks where the machine's leases are, so what a
// test's job holds alone is never what the production daemon's jobs hold, and no test touches the
// real lease directory.
func TestASandboxDaemonLeasesNothingWhereAnotherDaemonLooks(t *testing.T) {
	root := t.TempDir()
	runtime := func(name string) runtimedir.Dir {
		d, err := runtimedir.Open(filepath.Join(root, name), os.Getuid(), anyLocal{})
		if err != nil {
			t.Fatal(err)
		}
		return d
	}
	a, b := runtime("a"), runtime("b")
	refuse := func() (string, error) {
		t.Error("a sandbox daemon asked where the machine's leases are")
		return leasesDir, nil
	}
	scopeA, err := leaseScopeOf(true, a, refuse)
	if err != nil {
		t.Fatal(err)
	}
	scopeB, err := leaseScopeOf(true, b, refuse)
	if err != nil {
		t.Fatal(err)
	}
	if scopeA == scopeB {
		t.Fatalf("two sandboxes share %+v", scopeA)
	}
	p := Process{UID: 1000}
	for _, text := range configs {
		op, claims := parse(t, text), claimsOf(t, text)
		for resource, place := range p.leasingOf(op, scopeA, claims, clock.System{}, nil).Book.(lease.Map) {
			switch l := place.Leases.(type) {
			case locallease.Dir:
				if !strings.HasPrefix(l.Path, a.Path()+"/") {
					t.Errorf("config %q: %s is leased in %s, outside the sandbox's runtime directory %s", text, resource, l.Path, a.Path())
				}
			case hostlease.Host:
				if l.DirName == runtimedir.LeaseDirName || l.DirName == "" {
					t.Errorf("config %q: %s is leased on %s in the shared %q", text, resource, l.Shell.Where(), l.DirName)
				}
			case kubelease.Cluster:
				if l.Prefix == "" || l.Prefix == "vibesys-" {
					t.Errorf("config %q: %s is leased in a cluster under the shared prefix %q", text, resource, l.Prefix)
				}
			default:
				t.Errorf("config %q: %s is leased by a %T this test does not know", text, resource, l)
			}
		}
	}

	// A production daemon's scope is where the machine says, whatever its runtime directory is.
	got, err := leaseScopeOf(false, a, func() (string, error) { return leasesDir, nil })
	if err != nil || got != shared {
		t.Fatalf("a production daemon's scope = %+v, %v; want %+v", got, err, shared)
	}
}

// anyLocal is a file system with no network file system.
type anyLocal struct{}

func (anyLocal) IsNetwork(string) (bool, error) { return false, nil }
