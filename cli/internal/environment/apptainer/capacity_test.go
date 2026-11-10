package apptainer_test

import (
	"context"
	"strings"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

func capacityOf(t *testing.T, f apptainer.PartitionFacts, q apptainer.CapacityQuery) (names []string, usable map[string]bool) {
	t.Helper()
	cluster := clusterfake.New(fakeworld.New().MemoryGates)
	cluster.SetPartition(f)
	q.Cluster = cluster.Connect()
	c, _, err := apptainer.Capacity(context.Background(), q)
	if err != nil {
		t.Fatal(err)
	}
	usable = map[string]bool{}
	for _, r := range c.GetSlurm().GetReservations() {
		names = append(names, r.GetName())
		usable[r.GetName()] = r.GetUsable()
	}
	return names, usable
}

// A reservation counts for the partition when it names it or holds one of its nodes, and not once it
// is over; it is usable when the user or the account is named, or when only others are excluded.
func TestWhichReservationsCountAndWhoMayUseThem(t *testing.T) {
	now := clusterfake.Epoch
	hour := func(n int) time.Time { return now.Add(time.Duration(n) * time.Hour) }
	res := func(name string, o func(*apptainer.ReservationFacts)) apptainer.ReservationFacts {
		r := apptainer.ReservationFacts{Name: name, Start: hour(1), End: hour(2), NodeCount: 1}
		o(&r)
		return r
	}
	f := apptainer.PartitionFacts{
		Now: now, User: "user-a", Nodes: []string{"n1", "n2"}, NodesTotal: 2, NodesIdle: 1,
		Reservations: []apptainer.ReservationFacts{
			res("by-name", func(r *apptainer.ReservationFacts) { r.Partition = "part-a"; r.Users = []string{"user-a"} }),
			res("by-node", func(r *apptainer.ReservationFacts) { r.Nodes = []string{"n2"}; r.Accounts = []string{"acct-a"} }),
			res("one-of-two-partitions", func(r *apptainer.ReservationFacts) {
				r.Partition = "part-b,part-a"
				r.Users = []string{"user-b", "user-a"}
			}),
			res("elsewhere", func(r *apptainer.ReservationFacts) {
				r.Partition = "part-b"
				r.Nodes = []string{"n9"}
				r.Users = []string{"user-a"}
			}),
			res("over", func(r *apptainer.ReservationFacts) { r.Partition = "part-a"; r.Start, r.End = hour(-3), hour(-1) }),
			res("backwards", func(r *apptainer.ReservationFacts) { r.Partition = "part-a"; r.End = hour(0) }),
			res("somebody-else", func(r *apptainer.ReservationFacts) { r.Partition = "part-a"; r.Users = []string{"user-b"} }),
			res("not-us", func(r *apptainer.ReservationFacts) {
				r.Partition = "part-a"
				r.Users = []string{"-user-a"}
				r.Accounts = []string{"acct-a"}
			}),
			res("not-our-account", func(r *apptainer.ReservationFacts) { r.Partition = "part-a"; r.Accounts = []string{"-acct-a"} }),
			res("everyone-else", func(r *apptainer.ReservationFacts) { r.Partition = "part-a"; r.Accounts = []string{"-acct-z"} }),
			res("for-groups", func(r *apptainer.ReservationFacts) { r.Partition = "part-a"; r.Groups = []string{"staff"} }),
			res("for-nobody-we-know", func(r *apptainer.ReservationFacts) { r.Partition = "part-a" }),
		},
	}
	names, usable := capacityOf(t, f, apptainer.CapacityQuery{Placement: "p", Partition: "part-a", Account: "acct-a"})
	wantUsable := map[string]bool{
		"by-name": true, "by-node": true, "one-of-two-partitions": true, "somebody-else": false, "not-us": false,
		"not-our-account": false, "everyone-else": true, "for-groups": false, "for-nobody-we-know": false,
	}
	if len(usable) != len(wantUsable) {
		t.Errorf("reservations %v, want %d of them", names, len(wantUsable))
	}
	for name, want := range wantUsable {
		if got, ok := usable[name]; !ok || got != want {
			t.Errorf("%s: counted %v, usable %v; want usable %v", name, ok, got, want)
		}
	}
	// Without an account only a user's name makes one usable.
	_, noAccount := capacityOf(t, f, apptainer.CapacityQuery{Placement: "p", Partition: "part-a"})
	if noAccount["by-node"] || !noAccount["by-name"] {
		t.Errorf("with no account: by-node %v, by-name %v", noAccount["by-node"], noAccount["by-name"])
	}
}

// The recent waits come newest first, at most 256, and a sample that is not one (a negative wait, a
// request for no time) is left out; the slots are the partition's nodes, never more free than there are.
func TestCapacityCarriesTheRecentWaitsAndTheSlots(t *testing.T) {
	cluster := clusterfake.New(fakeworld.New().MemoryGates)
	cluster.SetPartition(apptainer.PartitionFacts{NodesTotal: 8, NodesIdle: 3, PendingJobs: 12})
	var waits []alloc.WaitSample
	for i := range 300 {
		waits = append(waits, alloc.WaitSample{RequestedAt: clusterfake.Epoch.Add(time.Duration(i) * time.Minute), Waited: time.Duration(i) * time.Second, RequestedFor: time.Hour})
	}
	waits = append(waits,
		alloc.WaitSample{RequestedAt: clusterfake.Epoch.Add(time.Hour), Waited: -time.Second, RequestedFor: time.Hour},
		alloc.WaitSample{RequestedAt: clusterfake.Epoch.Add(time.Hour), Waited: time.Second},
	)
	c, notes, err := apptainer.Capacity(context.Background(), apptainer.CapacityQuery{Cluster: cluster.Connect(), Placement: "p", Waits: waits})
	if err != nil || len(notes) != 0 {
		t.Fatal(err, notes)
	}
	if c.GetSlotsTotal() != 8 || c.GetSlotsFree() != 3 || c.GetSlurm().GetPendingJobs() != 12 {
		t.Errorf("capacity %v", c)
	}
	got := c.GetRecentWaits()
	if len(got) != apptainer.MaxRecentWaits || got[0].GetWaited().AsDuration() != 299*time.Second {
		t.Fatalf("%d waits, the first %v; want %d, newest first", len(got), got[0].GetWaited().AsDuration(), apptainer.MaxRecentWaits)
	}
	for i := 1; i < len(got); i++ {
		if got[i].GetRequestedAt().AsTime().After(got[i-1].GetRequestedAt().AsTime()) {
			t.Fatalf("wait %d is newer than wait %d", i, i-1)
		}
	}
}

// A cluster that cannot be reached has no capacity to report: the error is unavailable.
func TestCapacityOfAnUnreachableClusterIsUnavailable(t *testing.T) {
	cluster := clusterfake.New(fakeworld.New().MemoryGates)
	cut(cluster, true)
	_, _, err := apptainer.Capacity(context.Background(), apptainer.CapacityQuery{Cluster: cluster.Connect(), Placement: "p"})
	if f := core.AsFailure(err); err == nil || f.Kind != core.FailureUnavailable {
		t.Fatalf("Capacity = %v, want unavailable", err)
	}
}

// Whatever the cluster says of its nodes, the slots free are never more than the slots there are.
func TestCapacitySlotsAreNeverMoreFreeThanThereAre(t *testing.T) {
	if err := quick.Check(func(total, idle, pending uint32) bool {
		cluster := clusterfake.New(fakeworld.New().MemoryGates)
		cluster.SetPartition(apptainer.PartitionFacts{NodesTotal: total, NodesIdle: idle, PendingJobs: pending})
		c, _, err := apptainer.Capacity(context.Background(), apptainer.CapacityQuery{Cluster: cluster.Connect(), Placement: "p"})
		return err == nil && c.GetSlotsFree() <= c.GetSlotsTotal() && c.GetSlurm().GetNodesIdle() <= c.GetSlurm().GetNodesTotal()
	}, nil); err != nil {
		t.Error(err)
	}
}

func TestAccountOfTheSubmitOptions(t *testing.T) {
	for opts, want := range map[string]string{
		"":                                  "",
		"--partition=p":                     "",
		"--account=acct-a":                  "acct-a",
		"--partition=p --account acct-b":    "acct-b",
		"-A acct-c":                         "acct-c",
		"-Aacct-d":                          "acct-d",
		"--account=acct-a --account=acct-e": "acct-e",
		"--account":                         "",
		"--accounting=x":                    "",
	} {
		var o []string
		if opts != "" {
			o = strings.Fields(opts)
		}
		if got := apptainer.AccountOf(o); got != want {
			t.Errorf("AccountOf(%q) = %q, want %q", opts, got, want)
		}
	}
}
