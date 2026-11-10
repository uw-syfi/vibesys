package main

import (
	"context"
	"fmt"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

const placementUsage = "placement: name a subcommand: capacity"

// placementCommand is `placement capacity <placement>`: what the daemon measured of a placement's
// capacity, facts and no predictions. It takes no task.
func placementCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("%s", placementUsage))
	}
	if args[0] == "capacity" {
		return placementCapacity(ctx, d, args[1:])
	}
	return fail(d, d.jsonErrors, usagef("placement: %q is not a subcommand (capacity)", args[0]))
}

// placementCapacity is `placement capacity <placement> [--run r]`: the slots of the placement, free
// and in all, what its cluster says of its partition, the run's allocations on it and how long
// recent allocations waited. --run names the run whose allocations are listed as held.
func placementCapacity(ctx context.Context, d deps, args []string) int {
	positional, c, code, ok := allocArgs(d, "placement capacity", args, nil)
	if !ok {
		return code
	}
	if len(positional) != 1 {
		return fail(d, c.json, usagef("placement capacity: give one placement, such as placement capacity mi300a"))
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ReadPlacementCapacity(ctx, &cliv1.ReadPlacementCapacityRequest{Placement: positional[0], RunId: c.run})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
		return exitOK
	}
	pc := resp.GetCapacity()
	fmt.Fprintf(d.stdout, "%s: %d of %d slots free\n", pc.GetPlacement(), pc.GetSlotsFree(), pc.GetSlotsTotal())
	if s := pc.GetSlurm(); s != nil {
		fmt.Fprintf(d.stdout, "  cluster: %d of %d nodes idle, %d jobs waiting\n", s.GetNodesIdle(), s.GetNodesTotal(), s.GetPendingJobs())
		for _, r := range s.GetReservations() {
			fmt.Fprintf(d.stdout, "  reservation %s: %d nodes, %s to %s, usable %t\n", r.GetName(), r.GetNodeCount(),
				r.GetStart().AsTime().UTC().Format(time.RFC3339), r.GetEnd().AsTime().UTC().Format(time.RFC3339), r.GetUsable())
		}
	}
	for _, a := range pc.GetHeld() {
		fmt.Fprintf(d.stdout, "  held: %s\n", describeAllocation(a))
	}
	for _, w := range pc.GetRecentWaits() {
		fmt.Fprintf(d.stdout, "  waited %s for %s, requested %s\n", w.GetWaited().AsDuration(), w.GetRequestedFor().AsDuration(), w.GetRequestedAt().AsTime().UTC().Format(time.RFC3339))
	}
	return exitOK
}
