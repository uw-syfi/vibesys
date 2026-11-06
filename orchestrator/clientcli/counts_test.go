package clientcli_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"

	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// unreadView is the view a host that has just started serves: it has read no job log yet, so it
// has no jobs, which is not the run having none.
func unreadView(run string, revision uint64) *pb.SearchState {
	return &pb.SearchState{RunId: run, Revision: revision, Health: &pb.SearchHealth{State: &pb.SearchHealth_Unreadable{
		Unreadable: &pb.Unreadable{Reason: pb.UnreadableReason_UNREADABLE_REASON_NO_DAEMON, Since: timestamppb.New(epoch)},
	}}}
}

func liveView(run string, revision uint64, finished int) *pb.SearchState {
	state := &pb.SearchState{RunId: run, Revision: revision, Health: &pb.SearchHealth{State: &pb.SearchHealth_LiveSince{LiveSince: timestamppb.New(epoch)}}}
	for i := range finished {
		state.Jobs = append(state.Jobs, jobOf(fmt.Sprintf("job-%d", i), cliv1.JobState_JOB_STATE_SUCCEEDED, i%2 == 0))
	}
	return state
}

func finishedCount(v *clientcli.View) int {
	n := 0
	for _, j := range v.Search.GetJobs() {
		if j.GetState() == cliv1.JobState_JOB_STATE_SUCCEEDED {
			n++
		}
	}
	return n
}

// Right after a host restart the search view is unreadable and empty until the host has read the
// job log again. A view that folds the stream never shows fewer finished jobs than it did, whatever
// order restarts and reads come in: this is the live finding, "checks 0 finished" between 1 and 2.
func TestAViewNeverShowsFewerFinishedJobsAfterAHostRestart(t *testing.T) {
	for seed := range uint64(40) {
		r := rand.New(rand.NewPCG(seed, 4))
		v := clientcli.NewView("run-1")
		finished, revision, best := 0, uint64(0), 0
		for range 30 {
			revision++
			var state *pb.SearchState
			switch r.IntN(3) {
			case 0: // The host restarted: it has read nothing.
				state = unreadView("run-1", revision)
			default:
				finished += r.IntN(3)
				state = liveView("run-1", revision, finished)
			}
			v.ApplySearch(state)
			if got := finishedCount(v); got < best {
				t.Fatalf("seed %d: %d finished jobs after %d", seed, got, best)
			} else {
				best = got
			}
		}
	}
}

// A run whose host has not read the job log yet says its counts are loading, and never that none
// finished.
func TestGetSaysTheCountsAreLoadingWhileTheHostHasReadNothing(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	frame := snapshotWithSearch(5, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)}, unreadView("run-1", 1))
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	got := w.run(context.Background(), "run", "get", "run-1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "get", got.stdout, "checks      loading", "measures    loading")
	if strings.Contains(got.stdout, "0 finished") {
		t.Errorf("a count of zero was shown before anything was read:\n%s", got.stdout)
	}
}

// Once the job log is read the counts are shown as they are.
func TestGetShowsTheCountsOnceTheHostHasRead(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	frame := snapshotWithSearch(5, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)}, liveView("run-1", 2, 3))
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	got := w.run(context.Background(), "run", "get", "run-1")
	contains(t, "get", got.stdout, "measures    2 finished, 0 failed")
}

// A start that failed before its lead ran anything is left out of `run list` and named when
// asked for, and `run get` still shows it as a start that never happened.
func TestListHidesStartsThatNeverOpenedUnlessAskedAndGetStillShowsThem(t *testing.T) {
	good := runOf("run-good", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	never := runOf("run-never", "/work/other", pb.RunStatus_RUN_STATUS_FAILED, 1)
	never.NeverOpened = true
	frame := snapshotFrame(5, false, []*pb.Run{good, never}, []*pb.Agent{leadOf("run-never", pb.AgentStatus_AGENT_STATUS_FAILED)})
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))

	got := w.run(context.Background(), "run", "list")
	contains(t, "list", got.stdout, "run-good", "1 start(s) that failed before the agent ran are hidden", "run list --all")
	if strings.Contains(got.stdout, "run-never") {
		t.Errorf("a start that never opened is listed:\n%s", got.stdout)
	}

	got = w.run(context.Background(), "run", "list", "--all")
	contains(t, "list --all", got.stdout, "run-good", "run-never", "failed (never started)")
	if strings.Contains(got.stdout, "hidden") {
		t.Errorf("--all still reports hidden runs:\n%s", got.stdout)
	}

	got = w.run(context.Background(), "run", "list", "--json")
	if strings.Contains(got.stdout, "run-never") {
		t.Errorf("--json lists a start that never opened:\n%s", got.stdout)
	}

	got = w.run(context.Background(), "run", "get", "run-never")
	contains(t, "get", got.stdout, "failed (never started)")
}
