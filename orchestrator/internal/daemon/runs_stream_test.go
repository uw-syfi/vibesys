package daemon_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// These tests hold a WatchRuns stream of detail SUMMARY to one rule over generated fleets: the stream is
// bounded in every frame and every burst, a run the snapshot left out is never half sent, and one run
// whose summary cannot be served never costs the stream the others.

// frameRun is the run a frame of a WatchRuns stream is about, or "" for a snapshot.
func frameRun(f *pb.WatchRunsResponse) string {
	_, change, search := client.DecodeWatchRuns(f)
	switch summary := client.WatchRunsSummary(f); {
	case change != nil:
		return change.GetRunId()
	case search != nil:
		return search.GetRunId()
	case summary != nil:
		return summary.GetRunId()
	}
	return ""
}

// clientModel is what a client holds of the runs: the ones it was told of and a summary of each, which
// is all the desktop app needs to show a run in the fleet.
type clientModel struct {
	runs      map[string]bool
	summaries map[string]*pb.RunSummary
	leftOut   uint32
}

func newClientModel() *clientModel {
	return &clientModel{runs: map[string]bool{}, summaries: map[string]*pb.RunSummary{}}
}

// apply folds frames in and fails the test when one passes a budget, when one is about a run in leftOut,
// or when after the frames the client holds a run without its summary. first says the frames are the
// burst that opens the stream, whose follow-ups after the snapshot are bounded as one.
func (m *clientModel) apply(t *testing.T, frames []*pb.WatchRunsResponse, leftOut map[string]bool, first bool) {
	t.Helper()
	followUp := 0
	for i, f := range frames {
		size := proto.Size(f)
		if size > daemon.MaxWatchFrameBytes {
			t.Fatalf("frame %d is %d bytes: the receive limit is %d", i, size, daemon.MaxWatchFrameBytes)
		}
		if run := frameRun(f); run != "" && leftOut[run] {
			t.Fatalf("frame %d is about run %s, which the snapshot left out", i, run)
		}
		snap, change, search := client.DecodeWatchRuns(f)
		summary := client.WatchRunsSummary(f)
		switch {
		case snap != nil:
			if size > daemon.MaxSnapshotBytes {
				t.Fatalf("a snapshot of %d bytes passes the budget of %d", size, daemon.MaxSnapshotBytes)
			}
			m.runs, m.summaries, m.leftOut = map[string]bool{}, map[string]*pb.RunSummary{}, snap.GetRunsLeftOut()
			for _, r := range snap.GetRuns() {
				m.runs[r.GetId()] = true
			}
			for _, s := range snap.GetSummaries() {
				m.summaries[s.GetRunId()] = s
			}
		case change != nil:
			if run := change.GetRun(); run != nil {
				m.runs[run.GetId()] = true
			}
			if first {
				followUp += size
			}
		case summary != nil:
			if err := wire.Validate(summary); err != nil {
				t.Fatalf("frame %d: %v", i, err)
			}
			m.summaries[summary.GetRunId()] = summary
			followUp += size
		case search != nil:
			followUp += size
		}
	}
	if first && followUp > daemon.MaxFollowUpBytes {
		t.Fatalf("the frames after the snapshot are %d bytes: a burst is bounded by %d", followUp, daemon.MaxFollowUpBytes)
	}
	for run := range m.runs {
		if m.summaries[run] == nil {
			t.Fatalf("the client holds run %s with no summary of it", run)
		}
	}
}

func isChange(f *pb.WatchRunsResponse) bool {
	_, change, _ := client.DecodeWatchRuns(f)
	return change != nil
}

// damage makes a run name a summary report that the store does not hold, as a damaged store would.
func damage(t *testing.T, rig *daemontest.Rig, run core.RunID) {
	t.Helper()
	current, _ := rig.Daemon.State().Run(run)
	current.SummaryReport = "a-message-that-is-gone"
	if _, err := rig.Store.Commit(store.Result{Events: []core.Event{{Run: run, At: rig.Clock.Now(), Body: current}}}); err != nil {
		t.Fatal(err)
	}
}

func bigReport(kind pb.ReportKind, bytes int) *pb.Command {
	return &pb.Command{Body: &pb.Command_PostReport{PostReport: &pb.PostReport{Report: &pb.Report{
		Kind: kind, Headline: "h", Changed: strings.Repeat("c", bytes),
	}}}}
}

// For any fleet (runs of every size, some finished, some searched, some with a summary report that cannot
// be served or is too large to send), every frame of the stream is inside the receive limit, the snapshot
// and the burst after it inside their budgets, nothing is sent about a run the snapshot left out however
// many events it receives, the client never holds a run without its summary, and the stream never fails.
func TestAGeneratedFleetNeverBreaksItsSummaryStream(t *testing.T) {
	for seed := range uint64(16) {
		t.Run(fmt.Sprint("seed ", seed), func(t *testing.T) {
			r := rand.New(rand.NewPCG(seed, 31))
			rig := daemontest.NewRig(t)
			searches := new(search.Store)
			rig.Search = searches
			rig.Daemon.Search = searches
			n := 6 + r.IntN(18)
			ids := startRuns(t, rig, n, func(int) int { return 0 }, 0)
			reportSizes := []int{0, 2 << 10, 40 << 10, 150 << 10, 200 << 10, daemon.MaxSummaryBytes + (50 << 10)}
			tooBig := map[string]bool{}
			for i, id := range ids {
				if size := reportSizes[r.IntN(len(reportSizes))]; size > 0 {
					post(t, rig, rig.Daemon.State().Agents(id)[0].ID, fmt.Sprint("report-", i), bigReport(pb.ReportKind_REPORT_KIND_PROGRESS, size))
					tooBig[string(id)] = size > daemon.MaxSummaryBytes
				}
				searches.Put(id, search.Fold(search.Facts{{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: 1, Body: strings.Repeat("p", r.IntN(search.MaxStateBytes/2))}}}}))
				if r.IntN(3) == 0 {
					if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprint("stop-", i), Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(id)}}}}); err != nil {
						t.Fatal(err)
					}
					rig.Settle(t)
				}
			}
			damaged := map[string]bool{}
			for range r.IntN(3) {
				id := ids[r.IntN(len(ids))]
				damage(t, rig, id)
				damaged[string(id)] = true
			}
			rig.Restart(t)

			w, first, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
			if err != nil {
				t.Fatalf("a bad run broke the stream: %v", err)
			}
			defer w.Close()
			model := newClientModel()
			model.apply(t, first, nil, true)
			snap := client.WatchRunsSnapshot(first[0])
			if len(snap.GetRuns())+int(snap.GetRunsLeftOut()) != n {
				t.Fatalf("%d runs, %d listed and %d left out", n, len(snap.GetRuns()), snap.GetRunsLeftOut())
			}
			leftOut := map[string]bool{}
			for _, id := range ids {
				leftOut[string(id)] = !model.runs[string(id)]
			}
			for _, s := range snap.GetSummaries() {
				if unavailable := damaged[s.GetRunId()] || tooBig[s.GetRunId()]; s.GetReportUnavailable() != unavailable || (unavailable && s.GetReport() != nil) {
					t.Fatalf("run %s: report unavailable %t, want %t", s.GetRunId(), s.GetReportUnavailable(), unavailable)
				}
			}

			// Every run, listed or not, receives events, and a new run begins.
			for i, id := range ids {
				lead := rig.Daemon.State().Agents(id)[0].ID
				if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
					Id: fmt.Sprint("more-", i), Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead), Text: "again"}},
				}}); err != nil {
					t.Fatal(err)
				}
				searches.Put(id, search.Fold(search.Facts{{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: 1, Body: strings.Repeat("q", r.IntN(search.MaxStateBytes/2))}}}}))
			}
			resp, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: "late", Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
				Repository: "/repo-late", Task: "task", FanoutLimit: 1, Brief: "go", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
			}}}})
			if err != nil {
				t.Fatal(err)
			}
			rig.Settle(t)
			frames, err := w.Poll()
			if err != nil {
				t.Fatalf("events broke the stream: %v", err)
			}
			model.apply(t, frames, leftOut, false)
			if !model.runs[resp.GetReceipt().GetRunId()] {
				t.Fatalf("the run that began after the snapshot is not held: %v", model.runs)
			}

			// A client that resumes is sent a bounded burst, or a snapshot that says what it leaves out.
			resumed, frames, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, rig.Daemon.Last()-uint64(r.IntN(5)), pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
			if err != nil {
				t.Fatalf("a resume broke: %v", err)
			}
			defer resumed.Close()
			followUp := 0
			for _, f := range frames {
				if proto.Size(f) > daemon.MaxWatchFrameBytes {
					t.Fatal("a frame of a resume passes the receive limit")
				}
				if client.WatchRunsSnapshot(f) == nil && !isChange(f) {
					followUp += proto.Size(f)
				}
			}
			if followUp > daemon.MaxFollowUpBytes {
				t.Fatalf("a resume sent %d bytes after its replay", followUp)
			}
		})
	}
}

// The same rule holds for a stream of whole histories: a run the snapshot left out is not sent in any frame.
func TestARunTheSnapshotLeftOutIsNotSentInAnyDetail(t *testing.T) {
	for _, detail := range []pb.SnapshotDetail{pb.SnapshotDetail_SNAPSHOT_DETAIL_UNSPECIFIED, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY} {
		t.Run(detail.String(), func(t *testing.T) {
			rig := daemontest.NewRig(t)
			searches := new(search.Store)
			rig.Search = searches
			rig.Daemon.Search = searches
			ids := startRuns(t, rig, 14, func(int) int { return 0 }, 0)
			for i, id := range ids {
				post(t, rig, rig.Daemon.State().Agents(id)[0].ID, fmt.Sprint("big-", i), bigReport(pb.ReportKind_REPORT_KIND_PROGRESS, 200<<10))
			}
			w, first, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, detail)
			if err != nil {
				t.Fatal(err)
			}
			defer w.Close()
			snap := client.WatchRunsSnapshot(first[0])
			if snap.GetRunsLeftOut() == 0 {
				t.Fatal("the budget was not exercised")
			}
			listed := map[string]bool{}
			for _, run := range snap.GetRuns() {
				listed[run.GetId()] = true
			}
			leftOut := map[string]bool{}
			for _, id := range ids {
				leftOut[string(id)] = !listed[string(id)]
			}
			for i, id := range ids {
				lead := rig.Daemon.State().Agents(id)[0].ID
				if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
					Id: fmt.Sprint("hello-", i), Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead), Text: "hello"}},
				}}); err != nil {
					t.Fatal(err)
				}
				searches.Put(id, search.Fold(nil))
			}
			rig.Settle(t)
			frames, err := w.Poll()
			if err != nil {
				t.Fatal(err)
			}
			if len(frames) == 0 {
				t.Fatal("the listed runs were sent nothing")
			}
			for _, f := range frames {
				if run := frameRun(f); leftOut[run] {
					t.Fatalf("a frame about run %s, which the snapshot left out", run)
				}
			}
		})
	}
}

// A summary report that the store does not hold, or cannot read, or that is too large to send, costs its
// run the report and says so; the stream opens, the other runs are whole, and the report comes back once the
// host can read it.
func TestASummaryReportThatCannotBeServedDegradesOnlyItsRun(t *testing.T) {
	rig := daemontest.NewRig(t)
	ids := startRuns(t, rig, 3, func(int) int { return 0 }, 0)
	reports := map[string]string{}
	for i, id := range ids {
		receipt := post(t, rig, rig.Daemon.State().Agents(id)[0].ID, fmt.Sprint("r-", i), reportCommand(pb.ReportKind_REPORT_KIND_PROGRESS, "the run"))
		reports[string(id)] = receipt.GetMessageId()
	}
	damage(t, rig, ids[0])
	rig.Restart(t)
	rig.Faults.FailMessage = 1

	w, first, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatalf("a run whose report cannot be served failed the stream: %v", err)
	}
	defer w.Close()
	byRun := map[string]*pb.RunSummary{}
	for _, s := range client.WatchRunsSnapshot(first[0]).GetSummaries() {
		byRun[s.GetRunId()] = s
		if err := wire.Validate(s); err != nil {
			t.Fatal(err)
		}
	}
	unavailable := 0
	for id, s := range byRun {
		if s.GetReportUnavailable() {
			unavailable++
			if s.GetReport() != nil || s.GetAgentsTotal() == 0 {
				t.Fatalf("run %s: an unavailable report leaves the rest of the summary whole: %v", id, s)
			}
		} else if s.GetReport().GetId() != reports[id] {
			t.Fatalf("run %s: report %s, want %s", id, s.GetReport().GetId(), reports[id])
		}
	}
	if len(byRun) != 3 || unavailable != 2 {
		t.Fatalf("%d summaries, %d of them without a report: want 3 and 2", len(byRun), unavailable)
	}

	// The read that failed once works the next time the run is touched, and that summary has the report.
	var unread core.RunID
	for _, id := range ids[1:] {
		if byRun[string(id)].GetReportUnavailable() {
			unread = id
		}
	}
	post(t, rig, rig.Daemon.State().Agents(unread)[0].ID, "again", reportCommand(pb.ReportKind_REPORT_KIND_PROGRESS, "the run, again"))
	frames, err := w.Poll()
	if err != nil {
		t.Fatal(err)
	}
	got := summariesIn(frames)
	if len(got) == 0 || got[len(got)-1].GetReportUnavailable() || got[len(got)-1].GetReport() == nil {
		t.Fatalf("after the host could read it the summary is %v", got)
	}
}

// A run recorded before its summary report was named is named when the host opens its store, so a snapshot
// reads no mailbox: it serves the same summaries when every read of messages fails.
func TestASnapshotReadsNoMailbox(t *testing.T) {
	rig := daemontest.NewRig(t)
	ids := startRuns(t, rig, 4, func(int) int { return 3 }, 100)
	want := map[string]string{}
	for i, id := range ids {
		receipt := post(t, rig, rig.Daemon.State().Agents(id)[0].ID, fmt.Sprint("r-", i), reportCommand(pb.ReportKind_REPORT_KIND_PROGRESS, "the run"))
		want[string(id)] = receipt.GetMessageId()
		current, _ := rig.Daemon.State().Run(id)
		current.SummaryReport = "" // as an earlier build stored it
		if _, err := rig.Store.Commit(store.Result{Events: []core.Event{{Run: id, At: rig.Clock.Now(), Body: current}}}); err != nil {
			t.Fatal(err)
		}
	}
	rig.Restart(t)
	rig.Faults.FailMessages = -1

	_, first, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatalf("the snapshot read a mailbox: %v", err)
	}
	for _, s := range client.WatchRunsSnapshot(first[0]).GetSummaries() {
		if s.GetReportUnavailable() || s.GetReport().GetId() != want[s.GetRunId()] {
			t.Fatalf("run %s is summed up by %v, want %s", s.GetRunId(), s, want[s.GetRunId()])
		}
	}
}

// The first page of a run's history is bounded as a whole, and counts what it leaves out: for any run, what
// the pages hold and what they count add up to what the run has, and no page passes the budget except by
// the one message it must hold to make progress.
func TestAHistoryPageIsBoundedAsAWholeAndCountsWhatItLeavesOut(t *testing.T) {
	for seed := range uint64(10) {
		t.Run(fmt.Sprint("seed ", seed), func(t *testing.T) {
			r := rand.New(rand.NewPCG(seed, 17))
			rig := daemontest.NewRig(t)
			resp, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: "start", Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
				Repository: "/repo", Task: "task", FanoutLimit: 200, Brief: "go", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
			}}}})
			if err != nil {
				t.Fatal(err)
			}
			rig.Settle(t)
			run := core.RunID(resp.GetReceipt().GetRunId())
			lead := rig.Daemon.State().Agents(run)[0].ID
			for i := range 10 + r.IntN(60) {
				post(t, rig, lead, fmt.Sprint("spawn-", i), &pb.Command{Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{RunId: string(run), Role: pb.AgentRole_AGENT_ROLE_WORKER, Brief: "work"}}})
			}
			for range r.IntN(8) {
				rig.Clock.Advance(time.Second)
				if _, err := press(t, viewing(ann), rig, run); err != nil {
					t.Fatal(err)
				}
				rig.Settle(t)
			}
			budget := 6<<10 + r.IntN(30<<10)
			svc, err := daemon.NewService(rig.Daemon.Engine, daemon.ServiceDeps{
				Authority: rig.Auth, Audience: daemon.AudienceOperator, Bounds: rig.Bounds, Binary: "test", Starts: rig.Starts, HistoryPageBytes: budget,
			})
			if err != nil {
				t.Fatal(err)
			}
			state := rig.Daemon.State()
			turns, err := rig.Store.Turns(run, time.Time{}, time.Date(9999, time.December, 31, 0, 0, 0, 0, time.UTC), 0)
			if err != nil {
				t.Fatal(err)
			}
			wantMessages, err := allMessages(rig, run)
			if err != nil {
				t.Fatal(err)
			}
			var got []string
			for cursor, pages := uint64(0), 0; ; pages++ {
				page, err := svc.GetRunHistory(context.Background(), &pb.GetRunHistoryRequest{RunId: string(run), Cursor: cursor})
				if err != nil {
					t.Fatal(err)
				}
				if err := wire.Validate(page); err != nil {
					t.Fatal(err)
				}
				if size := proto.Size(page); size > budget && len(page.GetMessages()) > 1 {
					t.Fatalf("page %d is %d bytes with %d messages: the budget is %d", pages, size, len(page.GetMessages()), budget)
				}
				if pages == 0 {
					fixed := 0
					for _, a := range page.GetAgents() {
						fixed += proto.Size(a)
					}
					for _, turn := range page.GetTurns() {
						fixed += proto.Size(turn)
					}
					for _, c := range page.GetCheckIns() {
						fixed += proto.Size(c)
					}
					if fixed > budget/2 {
						t.Fatalf("the agents, turns and check-ins take %d bytes of a budget of %d", fixed, budget)
					}
					if total := len(state.Agents(run)); len(page.GetAgents())+int(page.GetAgentsLeftOutCount()) != total {
						t.Fatalf("%d agents held and %d counted left out of %d", len(page.GetAgents()), page.GetAgentsLeftOutCount(), total)
					}
					if total := len(turns.Turns) + turns.LeftOut; len(page.GetTurns())+int(page.GetTurnsLeftOutCount()) != total {
						t.Fatalf("%d turns held and %d counted left out of %d", len(page.GetTurns()), page.GetTurnsLeftOutCount(), total)
					}
					if total := len(state.CheckIns(run)); len(page.GetCheckIns())+int(page.GetCheckInsLeftOutCount()) != total {
						t.Fatalf("%d check-ins held and %d counted left out of %d", len(page.GetCheckIns()), page.GetCheckInsLeftOutCount(), total)
					}
					if page.GetAgentsLeftOutCount() == 0 && budget < 8<<10 && len(state.Agents(run)) > 40 {
						t.Fatalf("the budget %d held all %d agents", budget, len(state.Agents(run)))
					}
				} else if len(page.GetAgents())+len(page.GetTurns())+len(page.GetCheckIns()) != 0 {
					t.Fatalf("page %d repeats what the first holds", pages)
				}
				for _, m := range page.GetMessages() {
					got = append(got, m.GetId())
				}
				if page.GetNextCursor() == 0 {
					break
				}
				if pages > 2000 {
					t.Fatal("the pages never end")
				}
				cursor = page.GetNextCursor()
			}
			if !slices.Equal(got, wantMessages) {
				t.Fatalf("the pages hold %d messages, the run has %d", len(got), len(wantMessages))
			}
		})
	}
}
