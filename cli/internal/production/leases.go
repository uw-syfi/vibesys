package production

import (
	"os"
	"os/user"
	"strconv"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/hostlease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/locallease"
	"github.com/uw-syfi/vibesys/cli/internal/neighbors"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// leasingOf is where the daemon leases what its jobs hold alone, so the daemons of every user that
// share the machine their containers run on take turns at it. Jobs claim one resource today,
// placement:default, which stands for the one placement of a config of one: it is leased at the
// machine the placement's engine runs on, in its shared directory of leases, this machine's for a
// local placement and the engine's machine, reached over ssh, for a remote one. A Slurm placement
// leases nothing, since Slurm's exclusive allocation arbitrates; nor does a config of several
// placements, whose jobs claim no placement of their own yet.
func (p Process) leasingOf(op *cliv1.OperatorConfig) app.Leasing {
	holder := core.LeaseHolder{User: userName(p.UID), PID: os.Getpid()}
	if host, err := os.Hostname(); err == nil {
		holder.Host = host
	}
	none := app.Leasing{Book: lease.NoBook{}, Holder: holder}
	placements := op.GetPlacements()
	if len(placements) != 1 {
		return none
	}
	var leases lease.Leases
	for _, pl := range placements {
		switch pl.GetKind() {
		case cliv1.PlacementKind_PLACEMENT_KIND_LOCAL:
			leases = locallease.Dir{Path: locallease.DefaultDir}
		case cliv1.PlacementKind_PLACEMENT_KIND_SSH:
			r := pl.GetRemote()
			leases = hostlease.Host{Shell: remote.SSH{Host: r.GetHost(), Program: orDefault(r.GetSshCommand(), "ssh")}, Dir: locallease.DefaultDir}
		case cliv1.PlacementKind_PLACEMENT_KIND_SLURM, cliv1.PlacementKind_PLACEMENT_KIND_UNSPECIFIED:
			return none
		}
	}
	return app.Leasing{
		Book:   lease.Map{string(schedule.DefaultPlacement): {Leases: leases, Key: "machine"}},
		Holder: holder,
	}
}

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
