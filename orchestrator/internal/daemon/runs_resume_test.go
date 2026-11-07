package daemon_test

import (
	"fmt"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// A client that resumes holds views that may be stale. When one burst can refresh every run it is sent that
// burst; when it cannot, it is sent a snapshot, which says how many runs it leaves out, and never a prefix
// of the refresh that would leave some views stale unannounced.
func TestAResumeThatOneBurstCannotRefreshIsAnsweredWithASnapshot(t *testing.T) {
	for _, tc := range []struct {
		name     string
		runs     int
		snapshot bool
	}{{"a small fleet", 3, false}, {"a fleet of heavy runs", 14, true}} {
		t.Run(tc.name, func(t *testing.T) {
			rig := daemontest.NewRig(t)
			ids := startRuns(t, rig, tc.runs, func(int) int { return 0 }, 0)
			for i, id := range ids {
				post(t, rig, rig.Daemon.State().Agents(id)[0].ID, fmt.Sprint("big-", i), bigReport(pb.ReportKind_REPORT_KIND_PROGRESS, 200<<10))
			}
			w, frames, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, rig.Daemon.Last(), pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
			if err != nil {
				t.Fatal(err)
			}
			defer w.Close()
			snap := len(frames) > 0 && client.WatchRunsSnapshot(frames[0]) != nil
			if snap != tc.snapshot {
				t.Fatalf("%d runs: a snapshot was sent: %t, want %t", tc.runs, snap, tc.snapshot)
			}
			newClientModel().apply(t, frames, nil, true)
			if snap && client.WatchRunsSnapshot(frames[0]).GetRunsLeftOut() == 0 {
				t.Fatal("a snapshot sent because the runs do not fit counts none left out")
			}
		})
	}
}
