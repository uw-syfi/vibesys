package apptainer

import (
	"context"
	"slices"
	"strings"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

// PartitionFacts is what Slurm says of a partition's capacity, read by one call of the Cluster: the
// facts only. What they mean for an allocation is Capacity's.
type PartitionFacts struct {
	// Now is the login node's clock when it was read.
	Now time.Time
	// User is the cluster user the reservations are checked against.
	User string
	// NodesTotal and NodesIdle count the partition's nodes, and the idle ones among them as Slurm
	// counts them, which includes a node it shows as reserved.
	NodesTotal, NodesIdle uint32
	// NodesReserved names the nodes Slurm shows in the state reserved: idle, but held by a
	// reservation that is under way.
	NodesReserved []string
	// Nodes names the partition's nodes.
	Nodes []string
	// PendingJobs counts the jobs, of any user, that wait for the partition.
	PendingJobs uint32
	// Reservations are every reservation of the cluster.
	Reservations []ReservationFacts
	// Skipped are the lines of Slurm's answers that could not be read, which no fact comes from.
	Skipped []string
}

// ReservationFacts is one reservation as scontrol says it.
type ReservationFacts struct {
	Name       string
	Start, End time.Time
	// Nodes names its nodes, and NodeCount counts them as Slurm does.
	Nodes     []string
	NodeCount uint32
	// Partition is the partition it names, if it names one.
	Partition string
	// Users, Accounts and Groups are who may use it, as Slurm lists them: a name that starts with
	// "-" is one that may not.
	Users, Accounts, Groups []string
}

// MaxRecentWaits bounds the wait samples a capacity carries.
const MaxRecentWaits = 256

// CapacityQuery is what Capacity reads.
type CapacityQuery struct {
	Cluster Cluster
	// Placement names the placement in the answer.
	Placement string
	// Partition is the partition the placement's allocations ask for; empty is the cluster's default.
	Partition string
	// Account is the Slurm account the placement's allocations are charged to, empty when they name
	// none: a reservation of accounts is then usable only when it names the user.
	Account string
	// Waits are how long recent allocations of the placement waited, in any order.
	Waits []alloc.WaitSample
}

// Capacity is what Slurm says of the placement's partition now, as a cliv1.PlacementCapacity with
// its Slurm detail: the nodes in all and idle, the jobs waiting, the reservations that are not over
// and that overlap the partition (by its name, or by a node of it), each with whether the account
// may use it, and the recent waits, newest first. The slots are the partition's nodes in all and
// idle: the caller, which knows what the run holds, says what of them is the run's. A node Slurm
// shows as reserved is not idle here unless a reservation that holds it is usable, as Slurm counts
// it idle whoever the reservation is for.
//
// A reservation is usable when the user or the account is named in it, or when it only excludes
// others and names neither; one that names groups alone is not taken to be, since the groups of the
// user are not asked. It also returns the lines of Slurm's answers that could not be read.
//
// A cluster that cannot be reached is an error of kind unavailable.
func Capacity(ctx context.Context, q CapacityQuery) (*cliv1.PlacementCapacity, []string, error) {
	f, err := q.Cluster.Partition(ctx, q.Partition)
	if err != nil {
		return nil, nil, err
	}
	idle := min(f.NodesIdle, f.NodesTotal)
	idle -= min(heldByOthers(f, q.Account), idle)
	out := &cliv1.PlacementCapacity{Placement: q.Placement, SlotsTotal: f.NodesTotal, SlotsFree: idle}
	slurm := &cliv1.SlurmCapacity{NodesTotal: f.NodesTotal, NodesIdle: idle, PendingJobs: f.PendingJobs}
	for _, r := range f.Reservations {
		if !r.End.After(f.Now) || !r.End.After(r.Start) || !overlaps(r, f, q.Partition) {
			continue
		}
		slurm.Reservations = append(slurm.Reservations, &cliv1.SlurmReservation{
			Name: r.Name, Start: timestamppb.New(r.Start), End: timestamppb.New(r.End), NodeCount: r.NodeCount,
			Usable: usable(r, f.User, q.Account),
		})
	}
	slices.SortStableFunc(slurm.Reservations, func(a, b *cliv1.SlurmReservation) int {
		return a.Start.AsTime().Compare(b.Start.AsTime())
	})
	if len(slurm.Reservations) > 256 {
		slurm.Reservations = slurm.Reservations[:256]
	}
	out.Detail = &cliv1.PlacementCapacity_Slurm{Slurm: slurm}
	waits := slices.DeleteFunc(slices.Clone(q.Waits), func(w alloc.WaitSample) bool { return w.Waited < 0 || w.RequestedFor <= 0 })
	slices.SortStableFunc(waits, func(a, b alloc.WaitSample) int { return b.RequestedAt.Compare(a.RequestedAt) })
	for _, w := range waits[:min(len(waits), MaxRecentWaits)] {
		out.RecentWaits = append(out.RecentWaits, &cliv1.WaitSample{
			RequestedAt: timestamppb.New(w.RequestedAt), Waited: durationpb.New(w.Waited), RequestedFor: durationpb.New(w.RequestedFor),
		})
	}
	return out, f.Skipped, nil
}

// heldByOthers counts the nodes Slurm counts idle that this user cannot use: those it shows reserved,
// unless a reservation under way that holds the node is one the user may use. A reserved node that
// no reservation of the answer holds is not taken to be usable either, since nothing says it is.
func heldByOthers(f PartitionFacts, account string) uint32 {
	var held uint32
	for _, n := range f.NodesReserved {
		mine := false
		for _, r := range f.Reservations {
			if !f.Now.Before(r.Start) && f.Now.Before(r.End) && slices.Contains(r.Nodes, n) && usable(r, f.User, account) {
				mine = true
				break
			}
		}
		if !mine {
			held++
		}
	}
	return held
}

// overlaps says whether the reservation holds nodes of the partition: it names the partition, or
// shares a node with it.
func overlaps(r ReservationFacts, f PartitionFacts, partition string) bool {
	if partition != "" && slices.Contains(strings.Split(r.Partition, ","), partition) {
		return true
	}
	for _, n := range r.Nodes {
		if slices.Contains(f.Nodes, n) {
			return true
		}
	}
	return false
}

// usable says whether the user, charged to the account, may run in the reservation.
func usable(r ReservationFacts, user, account string) bool {
	has := func(list []string, name string, negated bool) bool {
		if name == "" {
			return false
		}
		if negated {
			name = "-" + name
		}
		return slices.Contains(list, name)
	}
	if has(r.Users, user, true) || has(r.Accounts, account, true) {
		return false
	}
	if has(r.Users, user, false) || has(r.Accounts, account, false) {
		return true
	}
	// Only exclusions, and no group: whoever is not excluded may.
	only := func(list []string) bool {
		return !slices.ContainsFunc(list, func(s string) bool { return !strings.HasPrefix(s, "-") })
	}
	return only(r.Users) && only(r.Accounts) && len(r.Groups) == 0 && (len(r.Users) > 0 || len(r.Accounts) > 0)
}

// AccountOf is the account a list of submit options names (--account=X, --account X, -A X, -AX), the
// last one when several do, or "".
func AccountOf(options []string) string {
	var account string
	for i := 0; i < len(options); i++ {
		o := options[i]
		switch {
		case strings.HasPrefix(o, "--account="):
			account = strings.TrimPrefix(o, "--account=")
		case o == "--account" || o == "-A":
			if i+1 < len(options) {
				i++
				account = options[i]
			}
		case strings.HasPrefix(o, "-A") && !strings.HasPrefix(o, "--"):
			account = strings.TrimPrefix(o, "-A")
		}
	}
	return account
}
