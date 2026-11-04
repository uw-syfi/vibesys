package production

import (
	"crypto/sha256"
	"encoding/hex"
	"maps"
	"os"
	"os/user"
	"slices"
	"strconv"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/hostlease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/kubelease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/locallease"
	"github.com/uw-syfi/vibesys/cli/internal/neighbors"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// leasingOf is where the daemon leases what its jobs hold alone, so that the daemons of every user
// and every machine that share it take turns at it. Each placement's slots are leased as the
// machine its engine runs on, in that machine's shared directory of leases: this machine's for a
// local placement, and the engine's machine, reached over ssh, for an ssh one, so placements on one
// machine, of one daemon or of several, take turns there. Host jobs, which run on this machine, take
// this machine's lease when they claim a resource of their own. A Kubernetes cluster a placement
// gives is leased in the cluster itself, as a coordination.k8s.io Lease that kubectl writes on the
// engine's machine, so the daemons of machines that share the cluster take turns at it. GPUs need
// no lease of their own: they are a machine's, whose lease a job that holds them alone takes. A
// Slurm placement leases nothing, since Slurm's exclusive allocation arbitrates its nodes.
//
// scope says where each kind of lease is kept, so that a sandbox daemon's are its own. claims are what the resources of each placement claim, by
// placement name, as the scheduler has them.
func (p Process) leasingOf(op *cliv1.OperatorConfig, scope leaseScope, claims map[string][]core.Claim, clk clock.System, logf func(string, ...any)) app.Leasing {
	holder := core.LeaseHolder{User: userName(p.UID), PID: os.Getpid()}
	if host, err := os.Hostname(); err == nil {
		holder.Host = host
	}
	here := locallease.Dir{Path: scope.local}
	book := lease.Map{string(hostResource): {Leases: here, Key: machineKey}}
	for _, name := range slices.Sorted(maps.Keys(op.GetPlacements())) {
		pl := op.GetPlacements()[name]
		var shell remote.Shell
		var machine lease.Leases
		switch pl.GetKind() {
		case cliv1.PlacementKind_PLACEMENT_KIND_LOCAL:
			shell, machine = remote.Local{}, here
		case cliv1.PlacementKind_PLACEMENT_KIND_SSH:
			r := pl.GetRemote()
			ssh := remote.SSH{Host: r.GetHost(), Program: orDefault(r.GetSshCommand(), "ssh")}
			shell, machine = ssh, scope.remote(ssh)
		case cliv1.PlacementKind_PLACEMENT_KIND_SLURM, cliv1.PlacementKind_PLACEMENT_KIND_UNSPECIFIED:
			continue
		}
		book[string(schedule.PlacementOf(name))] = lease.Place{Leases: machine, Key: machineKey}
		k := pl.GetResources().GetKubernetes()
		if k == nil {
			continue
		}
		for _, c := range claims[name] {
			if strings.HasPrefix(c.Resource, string(core.ResourceKubernetes)+":") {
				book[c.Resource] = lease.Place{Key: clusterKey, Leases: kubelease.Cluster{
					API:    kubelease.Kubectl{Shell: shell, Kubeconfig: k.GetKubeconfig(), Timers: clk},
					Prefix: scope.clusterPrefix, Clock: clk, Timers: clk, Logf: logf,
				}}
			}
		}
	}
	return app.Leasing{Book: book, Holder: holder}
}

// leaseScope is where a daemon keeps each kind of lease. A production daemon's are shared by every
// daemon that can see the machine, the machines it reaches over ssh and the cluster; a sandbox
// daemon's are its own, so that a test's job never waits for the real benchmark's lease and the
// real one never waits for a test's.
type leaseScope struct {
	// local is the directory of this machine's leases.
	local string
	// remoteName is the name of the directory of leases on a machine reached over ssh, in the
	// first of the parents its own daemons use.
	remoteName string
	// clusterPrefix is what comes before a key in the name of a cluster's Lease object.
	clusterPrefix string
}

// leaseScopeOf is the scope of the daemon of dir. A sandbox daemon's is named by its runtime
// directory alone, and never asks where the machine's leases are; no variable moves a production
// daemon's, which is where resolve finds the machine's.
func leaseScopeOf(sandbox bool, dir runtimedir.Dir, resolve func() (string, error)) (leaseScope, error) {
	if sandbox {
		sum := sha256.Sum256([]byte(dir.Path()))
		id := hex.EncodeToString(sum[:8])
		return leaseScope{
			local:         dir.Leases(),
			remoteName:    runtimedir.LeaseDirName + "-sandbox-" + id,
			clusterPrefix: "vibesys-sandbox-" + id + "-",
		}, nil
	}
	local, err := resolve()
	if err != nil {
		return leaseScope{}, err
	}
	return leaseScope{local: local, remoteName: runtimedir.LeaseDirName}, nil
}

// remote are the leases of a machine reached over ssh, in the directory its own daemons resolve:
// the same parents in the same order, judged by the same network file system types.
func (s leaseScope) remote(shell remote.Shell) hostlease.Host {
	return hostlease.Host{
		Shell: shell, Parents: runtimedir.LeaseParents, DirName: s.remoteName, NetworkTypes: runtimedir.NetworkMagics(),
	}
}

// machineKey and clusterKey are the names of the leases of a machine and of a Kubernetes cluster.
const (
	machineKey lease.Key = "machine"
	clusterKey lease.Key = "cluster"
)

// neighborsOf lists the other daemons of this machine, of every user, as /proc shows them. A
// machine without /proc lists none.
func neighborsOf() []daemon.Neighbor {
	var out []daemon.Neighbor
	for _, n := range neighbors.Find(os.DirFS("/proc"), daemonName, os.Getpid()) {
		out = append(out, daemon.Neighbor{PID: n.PID, User: userName(n.UID)})
	}
	return out
}

// userName is the name of a user, or the number when the name is not known.
func userName(uid int) string {
	id := strconv.Itoa(uid)
	if u, err := user.LookupId(id); err == nil {
		return u.Username
	}
	return id
}
