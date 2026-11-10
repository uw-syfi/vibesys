package clustercli_test

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clustercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
)

func golden(t *testing.T, name string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// at is an instant of the golden files: 2026-11-10T16:00:00Z is what their login node's clock reads,
// 08:00 on the cluster's wall clock, eight hours behind.
func at(day, hour, min, sec int) time.Time {
	return time.Date(2026, 11, day, hour, min, sec, 0, time.UTC)
}

func isUnavailable(err error) bool {
	var f *core.Failure
	return errors.As(err, &f) && f.Kind == core.FailureUnavailable
}

// What squeue, sacct and scontrol say of a job is read into the job's state and times, with the
// cluster's local times made instants by the login node's own clock, whatever its zone.
func TestParseDetailReadsWhatSlurmSaysOfAJob(t *testing.T) {
	now := at(10, 16, 0, 0)
	for file, want := range map[string]apptainer.JobDetail{
		"detail_pending.txt":             {Phase: apptainer.Queued, Name: "PENDING", Reason: "Priority", EstimatedStart: at(11, 4, 30, 0)},
		"detail_pending_no_estimate.txt": {Phase: apptainer.Queued, Name: "PENDING", Reason: "ReqNodeNotAvail, May be reserved | for other job"},
		"detail_running.txt": {
			Phase: apptainer.Running, Name: "RUNNING", Reason: "None", Node: "node-a", StartedAt: now.Add(-30 * time.Minute), EndsAt: now.Add(90 * time.Minute),
		},
		"detail_running_days.txt": {
			Phase: apptainer.Running, Name: "RUNNING", Reason: "None", Node: "node-a", StartedAt: now.Add(-30 * time.Minute), EndsAt: now.Add(23*time.Hour + 30*time.Minute),
		},
		"detail_running_unlimited.txt":  {Phase: apptainer.Running, Name: "RUNNING", Reason: "None", Node: "node-a", StartedAt: now.Add(-30 * time.Minute)},
		"detail_timeout.txt":            {Phase: apptainer.Ended, Name: "TIMEOUT", TimeLimit: true, EndedAt: at(10, 15, 59, 58)},
		"detail_cancelled.txt":          {Phase: apptainer.Ended, Name: "CANCELLED", EndedAt: at(10, 15, 0, 0)},
		"detail_node_fail.txt":          {Phase: apptainer.Ended, Name: "NODE_FAIL", EndedAt: at(10, 15, 10, 0)},
		"detail_completing_timeout.txt": {Phase: apptainer.Ended, Name: "COMPLETING", Reason: "None", TimeLimit: true},
		"detail_scontrol_only.txt":      {Phase: apptainer.Ended, Name: "TIMEOUT", TimeLimit: true, EndedAt: at(10, 15, 59, 59)},
		"detail_unknown.txt":            {Phase: apptainer.Ended},
	} {
		t.Run(file, func(t *testing.T) {
			got, err := clustercli.ParseDetail(golden(t, file))
			if err != nil || got != want {
				t.Fatalf("ParseDetail = %+v, %v\nwant       %+v", got, err, want)
			}
		})
	}
}

// What is not an answer is unavailable, which says nothing of the job: a squeue that failed, a line
// that is not what was asked for, times Slurm did not write, records that disagree, a clock that is
// unreadable.
func TestParseDetailTakesMalformedAnswersForNoAnswer(t *testing.T) {
	for _, file := range []string{
		"detail_running_bad_time.txt", "detail_running_left_over_limit.txt", "detail_short_line.txt", "detail_squeue_failed.txt",
		"detail_still_live.txt", "detail_bad_clock.txt",
	} {
		t.Run(file, func(t *testing.T) {
			got, err := clustercli.ParseDetail(golden(t, file))
			if !isUnavailable(err) {
				t.Fatalf("ParseDetail = %+v, %v; want an unavailable failure", got, err)
			}
		})
	}
	// Whatever the text, the parser answers or fails, and never ends a job it did not read as ended.
	if err := quick.Check(func(s string) bool {
		d, err := clustercli.ParseDetail(s)
		return err != nil || d.Phase == apptainer.Ended && strings.Contains(s, "squeue") || strings.Contains(s, "squeue 0")
	}, nil); err != nil {
		t.Error(err)
	}
}

// Slurm's zone is whatever the cluster's is: the same readings an hour east or west are the same
// instants.
func TestParseDetailIsIndependentOfTheClustersZone(t *testing.T) {
	base := golden(t, "detail_running.txt")
	for _, offset := range []time.Duration{-8 * time.Hour, 0, 5*time.Hour + 30*time.Minute, 14 * time.Hour} {
		local := func(t time.Time) string { return t.Add(offset).Format("2006-01-02T15:04:05") }
		now := at(10, 16, 0, 0)
		text := strings.NewReplacer(
			"2026-11-10T08:00:00", local(now),
			"2026-11-10T07:30:00", local(now.Add(-30*time.Minute)),
		).Replace(base)
		got, err := clustercli.ParseDetail(text)
		if err != nil || !got.StartedAt.Equal(now.Add(-30*time.Minute)) || !got.EndsAt.Equal(now.Add(90*time.Minute)) {
			t.Errorf("a cluster %v ahead of UTC: %+v, %v", offset, got, err)
		}
	}
}

func TestParseTimeLeftAndDuration(t *testing.T) {
	for s, want := range map[string]time.Duration{
		"0:00": 0, "1:30": 90 * time.Second, "2:00:00": 2 * time.Hour, "1-00:00:00": 24 * time.Hour, "3-02:01:00": 74*time.Hour + time.Minute,
	} {
		if d, ok := clustercli.ParseDuration(s); !ok || d != want {
			t.Errorf("ParseDuration(%q) = %v, %v; want %v", s, d, ok, want)
		}
	}
	for _, s := range []string{"", "NOT_SET", "INVALID", "1-2:00", "x:00", "-1:00", "1:2:3:4", "1-"} {
		if d, ok := clustercli.ParseDuration(s); ok || d != 0 {
			t.Errorf("ParseDuration(%q) = %v, %v; want none", s, d, ok)
		}
	}
}

// The reservations, nodes and queue of a partition are read as scontrol, sinfo and squeue write
// them, and what is made of them is Capacity's: only the reservations that are not over and that hold
// nodes of the partition, each with whether the account may use it, soonest first.
func TestACapacityIsReadFromSlurmsAnswers(t *testing.T) {
	f, err := clustercli.ParsePartition(golden(t, "partition.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if f.NodesTotal != 6 || f.NodesIdle != 2 || f.PendingJobs != 3 || f.User != "user-a" || !f.Now.Equal(at(10, 16, 0, 0)) || len(f.Nodes) != 6 {
		t.Errorf("facts %+v", f)
	}
	if len(f.Skipped) != 2 || !strings.Contains(f.Skipped[0], "sometime") {
		t.Errorf("skipped %q, want the two lines that are no reservation", f.Skipped)
	}
	c, notes, err := apptainer.Capacity(context.Background(), apptainer.CapacityQuery{
		Cluster: partitionOf{f: f}, Placement: "p", Partition: "part-a", Account: "acct-a",
	})
	if err != nil || len(notes) != 2 {
		t.Fatalf("Capacity: %v, notes %q", err, notes)
	}
	if c.GetSlotsTotal() != 6 || c.GetSlotsFree() != 2 {
		t.Errorf("slots %d free of %d", c.GetSlotsFree(), c.GetSlotsTotal())
	}
	s := c.GetSlurm()
	if s.GetNodesTotal() != 6 || s.GetNodesIdle() != 2 || s.GetPendingJobs() != 3 {
		t.Errorf("slurm %v", s)
	}
	type res struct {
		name   string
		start  time.Time
		nodes  uint32
		usable bool
	}
	var got []res
	for _, r := range s.GetReservations() {
		got = append(got, res{r.GetName(), r.GetStart().AsTime(), r.GetNodeCount(), r.GetUsable()})
	}
	want := []res{
		{"all-but-one", at(11, 4, 0, 0), 1, true},
		{"of-a-group", at(11, 5, 0, 0), 1, false},
		{"excluded", at(11, 5, 0, 0), 1, false},
		{"ours", at(11, 16, 0, 0), 2, true},
		{"maint", at(12, 16, 0, 0), 4, false},
	}
	if !slices.Equal(got, want) {
		t.Errorf("reservations\n%+v\nwant\n%+v", got, want)
	}
}

// partitionOf is a cluster that knows the partition's facts alone.
type partitionOf struct {
	apptainer.Cluster
	f apptainer.PartitionFacts
}

func (p partitionOf) Partition(context.Context, string) (apptainer.PartitionFacts, error) {
	return p.f, nil
}

func TestAPartitionWithNoReservationsOrJobs(t *testing.T) {
	f, err := clustercli.ParsePartition(golden(t, "partition_none.txt"))
	if err != nil || f.PendingJobs != 0 || f.NodesTotal != 4 || f.NodesIdle != 4 || len(f.Reservations) != 0 || len(f.Skipped) != 0 {
		t.Fatalf("ParsePartition = %+v, %v", f, err)
	}
}

func TestAPartitionWhoseNodesCannotBeReadIsUnavailable(t *testing.T) {
	if f, err := clustercli.ParsePartition(golden(t, "partition_squeue_failed.txt")); !isUnavailable(err) {
		t.Fatalf("ParsePartition = %+v, %v; want unavailable", f, err)
	}
	if _, err := clustercli.ParsePartition("nothing"); !isUnavailable(err) {
		t.Fatalf("ParsePartition of nothing: %v", err)
	}
}

func TestExpandHosts(t *testing.T) {
	for in, want := range map[string][]string{
		"":                 nil,
		"(null)":           nil,
		"node1":            {"node1"},
		"node[01-03]":      {"node01", "node02", "node03"},
		"node[1,3-4],gpu7": {"node1", "node3", "node4", "gpu7"},
		"r[1-2]n[8-9]":     {"r1n8", "r1n9", "r2n8", "r2n9"},
		"a[08-10]":         {"a08", "a09", "a10"},
		"x[1],y":           {"x1", "y"},
	} {
		if got, ok := clustercli.ExpandHosts(in); !ok || !slices.Equal(got, want) {
			t.Errorf("ExpandHosts(%q) = %v, %v; want %v", in, got, ok, want)
		}
	}
	for _, in := range []string{"node[1-", "node[b-c]", "node[3-1]", "node]", "node[1-99999999]", "a,,b", "n[1-9999]m[1-9999]"} {
		if _, ok := clustercli.ExpandHosts(in); ok {
			t.Errorf("ExpandHosts(%q) was read", in)
		}
	}
	// Whatever the text, it ends, and makes no more names than it is allowed.
	if err := quick.Check(func(s string) bool {
		got, _ := clustercli.ExpandHosts(s)
		return len(got) <= clustercli.MaxHosts+1
	}, nil); err != nil {
		t.Error(err)
	}
}

// recording is a shell that answers every call with a canned output and writes down the argv it was
// given: the commands that reach the login node, exactly.
type recording struct {
	mu    sync.Mutex
	calls [][]string
	out   func(argv []string) string
}

func (r *recording) Command(ctx context.Context, argv []string) *exec.Cmd {
	r.mu.Lock()
	r.calls = append(r.calls, slices.Clone(argv))
	r.mu.Unlock()
	return exec.CommandContext(ctx, "/bin/sh", "-c", `cat >/dev/null; printf '%s' "$1"`, "vs-fake", r.out(argv))
}
func (*recording) Lost(int) bool              { return false }
func (*recording) Unreached(int, string) bool { return false }
func (*recording) Where() string              { return "host-a" }

func (r *recording) argvs() [][]string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return slices.Clone(r.calls)
}

func recorded(t *testing.T, out func(argv []string) string) (*clustercli.Cluster, *recording) {
	t.Helper()
	r := &recording{out: out}
	c, err := clustercli.New(clustercli.Config{Shell: r, Timers: envtest.NewTimers(), Submit: []string{"submit-job", "--site-flag"}})
	if err != nil {
		t.Fatal(err)
	}
	return c, r
}

// A standing allocation is submitted through the site's command, named and commented as one, never
// requeued, with the time in whole minutes, a node held by a sleep and no holder, nor a reaper.
func TestAStandingAllocationIsSubmittedWithTheSitesCommand(t *testing.T) {
	c, r := recorded(t, func([]string) string { return "4242;cluster-a\n" })
	id, err := c.Submit(context.Background(), apptainer.Allocation{
		Name: apptainer.StandingName("daemon1", "a7"), Comment: apptainer.RunComment("scope-a", "run-a"), Limit: 150 * time.Minute,
		Options: []string{"--partition=part-a", "--account=acct-a", "--exclusive"},
	})
	if err != nil || id != "4242" {
		t.Fatalf("Submit = %q, %v", id, err)
	}
	want := []string{
		"submit-job", "--site-flag", "--parsable", "--job-name=vibesys-apptainer-standing-daemon1-a7", "--comment=scope-a.run-a", "--no-requeue",
		"--time=150", "--nodes=1", "--ntasks=1", "--partition=part-a", "--account=acct-a", "--exclusive", "--output=/dev/null",
		"--wrap=exec sleep infinity",
	}
	if got := r.argvs(); len(got) != 1 || !slices.Equal(got[0], want) {
		t.Errorf("sent %q\nwant %q", got, want)
	}
}

// The calls that read a job's state, cancel it and read a partition each send one script with the
// job or partition as its argument, which asks for what the parsers read.
func TestTheStandingCallsSendTheCommandsTheParsersRead(t *testing.T) {
	c, r := recorded(t, func(argv []string) string {
		switch {
		case strings.Contains(argv[2], "squeue -h -j"):
			return golden(t, "detail_running.txt")
		case strings.Contains(argv[2], "scancel"):
			return ""
		}
		return golden(t, "partition_none.txt")
	})
	ctx := context.Background()
	if d, err := c.Detail(ctx, "4242"); err != nil || d.Node != "node-a" {
		t.Fatalf("Detail = %+v, %v", d, err)
	}
	if _, err := c.Partition(ctx, "part-a"); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Partition(ctx, ""); err != nil {
		t.Fatal(err)
	}
	calls := r.argvs()
	if len(calls) != 3 {
		t.Fatalf("%d calls: %q", len(calls), calls)
	}
	for i, wantArg := range []string{"4242", "part-a", ""} {
		if calls[i][0] != "/bin/sh" || calls[i][1] != "-c" || calls[i][2] == "" || calls[i][4] != wantArg || len(calls[i]) != 5 {
			t.Errorf("call %d: %q", i, calls[i])
		}
	}
	for call, needles := range map[int][]string{
		0: {`squeue -h -j "$1" -o '%T|%r|%S|%N|%L|%l'`, `sacct -n -X -P -j "$1" -o State,End`, `scontrol show job -o "$1"`},
		1: {`sinfo -h -p "$p" -o %F`, `sinfo -h -p "$p" -N -o %N`, `squeue -h -p "$p" -t PENDING -o %i`, `scontrol show reservation -o`, `id -un`},
	} {
		for _, n := range needles {
			if !strings.Contains(calls[call][2], n) {
				t.Errorf("the script of call %d lacks %q", call, n)
			}
		}
	}
	for _, bad := range []string{"4242;rm", "a b", "", "x"} {
		if _, err := c.Detail(ctx, bad); core.AsFailure(err).Kind != core.FailureInvalidInput {
			t.Errorf("Detail(%q) = %v, want invalid input", bad, err)
		}
		if err := c.CancelJob(ctx, bad); core.AsFailure(err).Kind != core.FailureInvalidInput {
			t.Errorf("CancelJob(%q) = %v, want invalid input", bad, err)
		}
	}
	if len(r.argvs()) != 3 {
		t.Error("a job id that is not one reached the login node")
	}
}

// A cancel is of the user's job of that id alone, then read until squeue and sacct say it ended.
func TestCancelJobCancelsByIDAndConfirms(t *testing.T) {
	cancelled := false
	c, r := recorded(t, func(argv []string) string {
		if strings.Contains(argv[2], "scancel") {
			cancelled = true
			return ""
		}
		if cancelled {
			return "squeue 1\nslurm_load_jobs error: Invalid job id specified\n==sacct==\nCANCELLED by 1002\n"
		}
		return "squeue 0\nRUNNING|None|N/A|node-a\n==sacct==\n"
	})
	if err := c.CancelJob(context.Background(), "4242"); err != nil {
		t.Fatal(err)
	}
	calls := r.argvs()
	if len(calls) < 2 || !strings.Contains(calls[0][2], `scancel -u "$(id -un)" "$1"`) || calls[0][4] != "4242" {
		t.Fatalf("calls %q, want a cancel of the user's job 4242 first", calls)
	}
	if !strings.Contains(calls[1][2], `squeue -h -j "$1"`) || calls[1][4] != "4242" {
		t.Errorf("the second call %q is no read of the job's state", calls[1])
	}
}

// A cancel that Slurm does not confirm is read again, and one the login node cannot take is
// unavailable.
func TestCancelJobFailsWhenTheLoginNodeDoesNotAnswer(t *testing.T) {
	sh := newShell(t)
	sh.exit = "255"
	sh.lost255 = true
	c := newCluster(t, sh)
	err := c.CancelJob(ctx(t), "4242")
	if !isUnavailable(err) {
		t.Fatalf("CancelJob = %v, want unavailable", err)
	}
}

// The scripts run in a real shell, with Slurm's programs faked: the state of a job is read through
// squeue, sacct and scontrol, a cancel is scancel of the id, and a partition is read through sinfo,
// squeue and scontrol, as the parsers expect.
func TestTheStandingScriptsRunInAShell(t *testing.T) {
	sh := newShell(t)
	fakes := map[string]string{
		"scancel":  "#!/bin/sh\necho \"$*\" >> \"$(dirname \"$0\")/scancel.log\"; touch \"$(dirname \"$0\")/cancelled\"\n",
		"squeue":   "#!/bin/sh\nfor a do case \"$a\" in -j) [ -e \"$(dirname \"$0\")/cancelled\" ] && { echo 'slurm_load_jobs error: Invalid job id specified'; exit 1; }; echo 'RUNNING|None|2026-11-10T07:30:00|node-a|1:30:00|2:00:00'; exit 0;; -t) printf '5001\\n5002\\n'; exit 0;; esac; done\n",
		"sacct":    "#!/bin/sh\nexit 0\n",
		"scontrol": "#!/bin/sh\nif [ \"$2\" = reservation ]; then echo 'ReservationName=r StartTime=2030-01-01T00:00:00 EndTime=2030-01-02T00:00:00 Nodes=n[1-2] NodeCnt=2 PartitionName=part-a Users=user-a'; fi\nexit 0\n",
		"sinfo":    "#!/bin/sh\nfor a do case \"$a\" in -N) printf 'n1\\nn2\\n'; exit 0;; esac; done\necho '1/1/0/2'\n",
	}
	for prog, text := range fakes {
		if err := os.WriteFile(filepath.Join(sh.bin, prog), []byte(text), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	c := newCluster(t, sh)
	d, err := c.Detail(ctx(t), "4242")
	if err != nil || d.Phase != apptainer.Running || d.Node != "node-a" || d.EndsAt.Sub(d.StartedAt) != 2*time.Hour {
		t.Fatalf("Detail = %+v, %v", d, err)
	}
	if err := c.CancelJob(ctx(t), "4242"); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(filepath.Join(sh.bin, "scancel.log")); !strings.Contains(string(got), "4242") || !strings.Contains(string(got), "-u") {
		t.Errorf("scancel was run as %q, want a cancel of the user's job 4242", got)
	}
	f, err := c.Partition(ctx(t), "part-a")
	if err != nil || f.NodesTotal != 2 || f.NodesIdle != 1 || f.PendingJobs != 2 || len(f.Nodes) != 2 || len(f.Reservations) != 1 || f.User == "" {
		t.Fatalf("Partition = %+v, %v", f, err)
	}
	if r := f.Reservations[0]; r.Name != "r" || r.NodeCount != 2 || r.Partition != "part-a" || !slices.Equal(r.Nodes, []string{"n1", "n2"}) {
		t.Errorf("reservation %+v", r)
	}
	if f.Now.IsZero() {
		t.Errorf("the login node's clock reads %v", f.Now)
	}
}

// Reap kills the box's processes again each second until none is left, and says so when some are.
func TestReapKillsEveryProcessOfTheBoxAndWaitsForNone(t *testing.T) {
	c, r := recorded(t, func([]string) string { return "" })
	if err := c.Node("77").Reap(context.Background(), "vibesys-cli-a"); err != nil {
		t.Fatal(err)
	}
	calls := r.argvs()
	if len(calls) != 1 {
		t.Fatalf("%d calls", len(calls))
	}
	argv := calls[0]
	// The script goes to the node as a step of the allocation, with the box as its argument.
	if len(argv) < 5 || !strings.HasSuffix(strings.Join(argv, " "), "vibesys-cli-a") || !slices.Contains(argv, "--jobid=77") {
		t.Fatalf("sent %q, want a step of the job 77", argv)
	}
	script := fmt.Sprint(argv)
	for _, need := range []string{"VIBESYS_BOX=$1", "kill -s KILL", "sleep 1", "-ge 60"} {
		if !strings.Contains(script, need) {
			t.Errorf("the reap script lacks %q", need)
		}
	}
	failing := newShell(t)
	failing.exit = "1"
	if err := newCluster(t, failing).Node("77").Reap(ctx(t), "b"); err == nil {
		t.Error("a reap that failed was taken for one that did not")
	}
}
