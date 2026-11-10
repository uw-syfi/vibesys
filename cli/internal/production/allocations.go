package production

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"maps"
	"path/filepath"
	"slices"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/host"
)

// defaultFitMargin is the time a job's fit keeps free before its allocation ends, when the config
// does not say (placements.<name>.allocations.fit_margin).
const defaultFitMargin = 2 * time.Minute

// defaultRunIdleEnd is how long a run holding allocations may make no call before it is taken to
// have ended, when the config does not say (placements.<name>.allocations.run_idle_end).
const defaultRunIdleEnd = 2 * time.Hour

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

// AllocationsDir is where one daemon keeps its allocation log and its identity, under the index
// directory of a state home: keyed by the daemon (host.DaemonKey), as its run records are, since a
// state home can be shared by several daemons, on one machine or on several, and two daemons that
// shared a log or an identity would number their allocations alike, overwrite each other's inputs,
// and adopt and release each other's allocations at a backend they share.
func AllocationsDir(indexDir, machine, runtimeDir string) string {
	return filepath.Join(indexDir, "daemons", host.DaemonKey(machine, runtimeDir))
}

// allocationsOf is how the daemon keeps standing allocations: the placements of the operator config
// that have an allocations section, each with the limits it gives, over the allocation log in the
// daemon's own directory (AllocationsDir). The identity the daemon keeps there is the Owner every
// allocator is made with, so that after a restart a backend returns only this daemon's allocations.
//
// A placement whose capacity is slots of a machine, local or over ssh, takes its allocations as
// slots of its own max_jobs, so it needs no allocator of its own and its allocations have no end.
// A placement of kind apptainer, whose capacity is a cluster's batch allocations, gets an
// apptainer.Allocator made with the Owner, over the cluster, scope and sizing its engine uses, and
// its allocations are Slurm's. Its engine's docker.Config.AllocationLabel is
// apptainer.LabelAllocation, so that a job started in an allocation runs inside it.
//
// A placement of kind slurm has no allocator: its containers run in one Docker daemon outside the
// allocations Slurm makes, so it refuses to start with an allocations section, which says so,
// instead of keeping allocations it cannot back.
func allocationsOf(op *cliv1.OperatorConfig, daemonDir string, clusters map[string]clusterSide) (app.Allocations, error) {
	var out app.Allocations
	names := slices.Sorted(maps.Keys(op.GetPlacements()))
	var owner string
	var former []string
	for _, name := range names {
		pl := op.GetPlacements()[name]
		a := pl.GetAllocations()
		if a == nil {
			if pl.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER {
				former = append(former, name)
			}
			continue
		}
		var backend environment.Allocator
		switch pl.GetKind() {
		case cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, cliv1.PlacementKind_PLACEMENT_KIND_SSH:
		case cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER:
			if owner == "" {
				var err error
				if owner, err = filestore.DaemonID(daemonDir, newDaemonID); err != nil {
					return app.Allocations{}, fmt.Errorf("reading the daemon's identity: %w", err)
				}
			}
			side, ok := clusters[name]
			if !ok {
				return app.Allocations{}, fmt.Errorf("placements.%s is an apptainer placement the daemon made no cluster for", name)
			}
			allocator, err := apptainer.NewAllocator(apptainer.AllocatorConfig{
				Cluster: side.Cluster, Scope: side.Scope, Sizing: side.Sizing, MaxTime: side.MaxTime, Owner: owner, Clock: side.Clock,
			})
			if err != nil {
				return app.Allocations{}, fmt.Errorf("the operator config: %s: %w", alloc.ConfigKey(name), err)
			}
			backend = allocator
		case cliv1.PlacementKind_PLACEMENT_KIND_UNSPECIFIED, cliv1.PlacementKind_PLACEMENT_KIND_SLURM:
			return app.Allocations{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
				"the operator config: %s: this build keeps standing allocations for placements of kind local, ssh and apptainer only, and placements.%s is %s",
				alloc.ConfigKey(name), name, pl.GetKind())}
		}
		margin := defaultFitMargin
		if a.GetFitMargin() != nil {
			margin = a.GetFitMargin().AsDuration()
		}
		idleEnd := defaultRunIdleEnd
		if a.GetRunIdleEnd() != nil {
			idleEnd = a.GetRunIdleEnd().AsDuration()
		}
		limits := alloc.Limits{MaxHeld: int(a.GetMaxHeld()), MaxFor: a.GetMaxFor().AsDuration(), Margin: margin, Endless: backend == nil, RunIdleEnd: idleEnd}
		if backend != nil {
			// The queue is Slurm's: an allocation that waits past the placement's queue timeout ends
			// QUEUE_TIMEOUT, as a container's allocation fails past it. A machine's slots have no queue.
			limits.QueueTimeout = clusters[name].QueueTimeout
		}
		out.Placements = append(out.Placements, app.AllocPlacement{Name: name, Limits: limits, Allocator: backend, Slurm: backend != nil})
	}
	if len(out.Placements) == 0 {
		// A daemon whose config offers no allocations keeps none, unless it kept some before: then
		// it starts over its log to end and release them.
		kept, err := filestore.HasAllocLog(daemonDir)
		if err != nil {
			return app.Allocations{}, fmt.Errorf("looking for the allocation log: %w", err)
		}
		if !kept {
			return app.Allocations{}, nil
		}
	}
	log, err := filestore.OpenAllocLog(daemonDir)
	if err != nil {
		return app.Allocations{}, fmt.Errorf("opening the allocation log: %w", err)
	}
	out.Log = log
	if out.Owner, err = filestore.DaemonID(daemonDir, newDaemonID); err != nil {
		return app.Allocations{}, fmt.Errorf("reading the daemon's identity: %w", err)
	}
	// An apptainer placement with no allocations section may hold allocations this daemon made
	// before the section was removed: it is given an allocator, made with the same owner, that only
	// finds and releases them.
	for _, name := range former {
		side, ok := clusters[name]
		if !ok {
			continue
		}
		allocator, err := apptainer.NewAllocator(apptainer.AllocatorConfig{
			Cluster: side.Cluster, Scope: side.Scope, Sizing: side.Sizing, MaxTime: side.MaxTime, Owner: out.Owner,
		})
		if err != nil {
			return app.Allocations{}, fmt.Errorf("placements.%s: %w", name, err)
		}
		out.Former = append(out.Former, app.AllocPlacement{Name: name, Allocator: allocator, Slurm: true})
	}
	return out, nil
}

// capacitiesOf are the readers of the placements whose capacity is a cluster's, every apptainer
// placement whether or not it has an allocations section: what Slurm says of its partition, with
// the placement's recent waits. The slots are the partition's nodes, and the daemon adds the run's
// own allocations.
func capacitiesOf(clusters map[string]clusterSide) map[string]daemon.Capacity {
	out := map[string]daemon.Capacity{}
	for name, side := range clusters {
		out[name] = func(ctx context.Context, waits []alloc.WaitSample) (*cliv1.PlacementCapacity, error) {
			// The lines of Slurm's answers that could not be read have no place in the contract.
			c, _, err := apptainer.Capacity(ctx, apptainer.CapacityQuery{
				Cluster: side.Cluster, Placement: name, Partition: side.Partition, Waits: waits,
			})
			return c, err
		}
	}
	return out
}
