package production

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"maps"
	"slices"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
)

// defaultFitMargin is the time a job's fit keeps free before its allocation ends, when the config
// does not say (placements.<name>.allocations.fit_margin).
const defaultFitMargin = 2 * time.Minute

// newDaemonID makes the identity of a daemon: random, so that two users' daemons, or a daemon after
// its state directory was cleared, are never taken for one another.
func newDaemonID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// The system has no randomness; an identity that is not unique would let one daemon release
		// another's allocations, so the daemon does not start.
		panic(fmt.Sprintf("the daemon cannot make its identity: %v", err))
	}
	return hex.EncodeToString(b[:])
}

// allocationsOf is how the daemon keeps standing allocations: the placements of the operator config
// that have an allocations section, each with the limits it gives, over the allocation log in the
// index directory. The identity the daemon keeps there is the Owner every allocator is made with,
// so that after a restart a backend returns only this daemon's allocations.
//
// A placement whose capacity is slots of a machine, local or over ssh, takes its allocations as
// slots of its own max_jobs, so it needs no allocator of its own and its allocations have no end.
// A placement whose capacity is a cluster's batch allocations gets its allocator here, made with
// Owner: apptainer.NewAllocator(apptainer.AllocatorConfig{Cluster, Scope, Sizing, MaxTime, Owner}),
// set as AllocPlacement.Allocator, and its engine's docker.Config.AllocationLabel is
// apptainer.LabelAllocation, so that a job started in an allocation runs inside it. Until then such
// a placement refuses to start with an allocations section, which says so, instead of keeping
// allocations it cannot back.
func allocationsOf(op *cliv1.OperatorConfig, indexDir string) (app.Allocations, error) {
	var out app.Allocations
	names := slices.Sorted(maps.Keys(op.GetPlacements()))
	for _, name := range names {
		pl := op.GetPlacements()[name]
		a := pl.GetAllocations()
		if a == nil {
			continue
		}
		switch pl.GetKind() {
		case cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, cliv1.PlacementKind_PLACEMENT_KIND_SSH:
		case cliv1.PlacementKind_PLACEMENT_KIND_UNSPECIFIED, cliv1.PlacementKind_PLACEMENT_KIND_SLURM, cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER:
			return app.Allocations{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
				"the operator config: %s: this build keeps standing allocations for placements of kind local and ssh only, and placements.%s is %s",
				alloc.ConfigKey(name), name, pl.GetKind())}
		}
		margin := defaultFitMargin
		if a.GetFitMargin() != nil {
			margin = a.GetFitMargin().AsDuration()
		}
		out.Placements = append(out.Placements, app.AllocPlacement{Name: name, Limits: alloc.Limits{
			MaxHeld: int(a.GetMaxHeld()), MaxFor: a.GetMaxFor().AsDuration(), Margin: margin, Endless: true,
		}})
	}
	if len(out.Placements) == 0 {
		return app.Allocations{}, nil
	}
	log, err := filestore.OpenAllocLog(indexDir)
	if err != nil {
		return app.Allocations{}, fmt.Errorf("opening the allocation log: %w", err)
	}
	out.Log = log
	if out.Owner, err = filestore.DaemonID(indexDir, newDaemonID); err != nil {
		return app.Allocations{}, fmt.Errorf("reading the daemon's identity: %w", err)
	}
	return out, nil
}
