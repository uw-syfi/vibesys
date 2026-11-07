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

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// These tests hold what the host serves of a run (its RunSummary and the size of the snapshot that lists
// runs) to the rules the desktop app relies on: it reads these and derives none of them.

// post has an agent of a run call a tool through the service, with its own credentials.
func post(t *testing.T, rig *daemontest.Rig, agent core.AgentID, id string, body *pb.Command) *pb.Receipt {
	t.Helper()
	token := rig.Sidecars.Session(core.Session{Agent: agent, N: 1}).Credentials.GetToken()
	body.Id = id
	resp, err := rig.AgentService.Dispatch(agentContext(agent, 1, token), &pb.DispatchRequest{Command: body})
	if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("%s: %v, %v", id, resp, err)
	}
	rig.Settle(t)
	return resp.GetReceipt()
}

func reportCommand(kind pb.ReportKind, headline string) *pb.Command {
	return &pb.Command{Body: &pb.Command_PostReport{PostReport: &pb.PostReport{Report: &pb.Report{Kind: kind, Headline: headline}}}}
}

// summariesIn returns the summary frames of a poll, by run.
func summariesIn(frames []*pb.WatchRunsResponse) []*pb.RunSummary {
	var out []*pb.RunSummary
	for _, f := range frames {
		if s := client.WatchRunsSummary(f); s != nil {
			out = append(out, s)
		}
	}
	return out
}

// holdCatchUps makes a session leave a turn that Catch me up started open, so that the lead can post
// the report that answers it, and answer every other turn at once.
func holdCatchUps(rig *daemontest.Rig) {
	rig.Sidecars.React = func(s *daemontest.Session, cmd *pb.SessionCommand) {
		if turn, ok := cmd.GetBody().(*pb.SessionCommand_StartTurn); ok && strings.Contains(turn.StartTurn.GetPrompt(), "Catch me up") {
			return
		}
		daemontest.DefaultReaction(s, cmd)
	}
}

// A report that answers Catch me up covers a window, so it is never the report a run is summed up by,
// whether the client reads the summary in a snapshot or as it changes; the next report about the run is.
func TestACatchUpAnswerIsNotTheReportARunIsSummedUpBy(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, lead := beginRun(t, rig)
	holdCatchUps(rig)
	whole := post(t, rig, lead.ID, "whole", reportCommand(pb.ReportKind_REPORT_KIND_PROGRESS, "the run"))

	w, first, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	snap := client.WatchRunsSnapshot(first[0])
	if len(snap.GetSummaries()) != 1 || snap.GetSummaries()[0].GetReport().GetId() != whole.GetMessageId() ||
		len(snap.GetTurns())+len(snap.GetMessages())+len(snap.GetAgents()) != 0 {
		t.Fatalf("a summary snapshot of one run: %v", snap)
	}
	if err := wire.Validate(snap); err != nil {
		t.Fatal(err)
	}

	rig.Clock.Advance(time.Minute)
	if _, err := press(t, viewing(ann), rig, run.ID); err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	window := post(t, rig, lead.ID, "window", reportCommand(pb.ReportKind_REPORT_KIND_PROGRESS, "since you looked"))
	frames, err := w.Poll()
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range summariesIn(frames) {
		if s.GetReport().GetId() != whole.GetMessageId() {
			t.Fatalf("the answer to a catch-up %s became the summary %s", window.GetMessageId(), s.GetReport().GetId())
		}
		if err := wire.Validate(s); err != nil {
			t.Fatal(err)
		}
	}
	summary := summariesIn(frames)
	if len(summary) == 0 || summary[len(summary)-1].GetAgentsWorking() == 0 {
		t.Fatalf("the lead is in the turn that the request started, and the summary says so: %v", summary)
	}

	// A second report in that turn answers nothing: it is about the run, and it is the summary at once.
	again := post(t, rig, lead.ID, "again", reportCommand(pb.ReportKind_REPORT_KIND_PROGRESS, "the run, again"))
	frames, err = w.Poll()
	if err != nil {
		t.Fatal(err)
	}
	summary = summariesIn(frames)
	if len(summary) == 0 || summary[len(summary)-1].GetReport().GetId() != again.GetMessageId() {
		t.Fatalf("after a report about the run the summary is %v, want %s", summary, again.GetMessageId())
	}
	// And the same summary is what a client that connects now is told.
	_, late, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	if got := client.WatchRunsSnapshot(late[0]).GetSummaries()[0]; !proto.Equal(got, summary[len(summary)-1]) {
		t.Fatalf("a new connection is told %v, the stream said %v", got, summary[len(summary)-1])
	}
}

// A stream that did not ask for summaries is never sent a frame it does not read, and every stream's
// snapshot carries the summaries.
func TestOnlyAStreamThatAskedForSummariesIsSentSummaryFrames(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, lead := beginRun(t, rig)
	w, first, err := rig.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	if len(client.WatchRunsSnapshot(first[0]).GetSummaries()) != 1 || client.WatchRunsSnapshot(first[0]).GetSummaries()[0].GetRunId() != string(run.ID) {
		t.Fatalf("a snapshot of whole histories has no summaries: %v", client.WatchRunsSnapshot(first[0]))
	}
	post(t, rig, lead.ID, "r", reportCommand(pb.ReportKind_REPORT_KIND_FINAL, "done"))
	frames, err := w.Poll()
	if err != nil {
		t.Fatal(err)
	}
	if len(frames) == 0 || len(summariesIn(frames)) != 0 {
		t.Fatalf("frames %v", frames)
	}
}

// The agents of a run are counted by one definition: launching, in a turn, or waiting on subagents.
func TestARunSummaryCountsWorkingAgentsOnceForEveryScreen(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, lead := beginRun(t, rig)
	// A lead with a subagent that has not finished its turn is waiting, and so is working.
	rig.Sidecars.React = func(s *daemontest.Session, cmd *pb.SessionCommand) {
		if _, ok := cmd.GetBody().(*pb.SessionCommand_StartTurn); ok && s.Launch.Session.Agent != lead.ID {
			return
		}
		daemontest.DefaultReaction(s, cmd)
	}
	post(t, rig, lead.ID, "spawn", &pb.Command{Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{
		RunId: string(run.ID), Role: pb.AgentRole_AGENT_ROLE_WORKER, Brief: "work",
	}}})
	_, frames, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	summary := client.WatchRunsSnapshot(frames[0]).GetSummaries()[0]
	var statuses []core.AgentStatus
	for _, a := range rig.Daemon.State().Agents(run.ID) {
		statuses = append(statuses, a.Status)
	}
	if summary.GetAgentsTotal() != 2 || summary.GetAgentsWorking() != 2 || len(summary.GetRoles()) != 2 {
		t.Fatalf("a lead waiting on a working subagent: statuses %v, summary %v", statuses, summary)
	}
	for _, r := range summary.GetRoles() {
		if r.GetTotal() != 1 || r.GetWorking() != 1 {
			t.Fatalf("role %v", r)
		}
	}
}

// startRuns starts n runs in n repositories, each with a lead that is sent `messages` messages of `size`
// bytes, and returns them.
func startRuns(t *testing.T, rig *daemontest.Rig, n int, messages func(i int) int, size int) []core.RunID {
	t.Helper()
	var runs []core.RunID
	for i := range n {
		rig.Clock.Advance(time.Second)
		resp, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprintf("start-%d", i), Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
			Repository: fmt.Sprintf("/repo-%d", i), Task: "task", FanoutLimit: 1, Brief: "go", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
		}}}})
		if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
			t.Fatalf("start %d: %v, %v", i, resp, err)
		}
		rig.Settle(t)
		run := core.RunID(resp.GetReceipt().GetRunId())
		lead := rig.Daemon.State().Agents(run)[0].ID
		runs = append(runs, run)
		for j := range messages(i) {
			if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
				Id: fmt.Sprintf("say-%d-%d", i, j), Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead), Text: strings.Repeat("x", size)}},
			}}); err != nil {
				t.Fatal(err)
			}
		}
		rig.Settle(t)
	}
	return runs
}

// However long the mailboxes are, a snapshot of detail SUMMARY lists every run with its summary, holds
// nothing of any history, and is bounded by the number of runs; and a snapshot of whole histories lists
// every run too and says that it cut a history.
func TestASummarySnapshotIsBoundedByTheNumberOfRunsAndNeverDropsOne(t *testing.T) {
	for seed := range uint64(6) {
		t.Run(fmt.Sprint("seed ", seed), func(t *testing.T) {
			r := rand.New(rand.NewPCG(seed, 7))
			rig := daemontest.NewRig(t)
			runs := 3 + r.IntN(10)
			lengths := make([]int, runs)
			for i := range lengths {
				lengths[i] = r.IntN(70)
			}
			ids := startRuns(t, rig, runs, func(i int) int { return lengths[i] }, 2000+r.IntN(4000))

			_, frames, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
			if err != nil {
				t.Fatal(err)
			}
			snap := client.WatchRunsSnapshot(frames[0])
			if len(snap.GetRuns()) != runs || len(snap.GetSummaries()) != runs || snap.GetRunsLeftOut() != 0 || snap.GetTruncated() {
				t.Fatalf("%d runs listed as %d runs and %d summaries, %d left out, truncated %t", runs, len(snap.GetRuns()), len(snap.GetSummaries()), snap.GetRunsLeftOut(), snap.GetTruncated())
			}
			if len(snap.GetTurns())+len(snap.GetMessages())+len(snap.GetAgents())+len(snap.GetCheckIns()) != 0 {
				t.Fatalf("a summary snapshot holds a history: %d turns, %d messages", len(snap.GetTurns()), len(snap.GetMessages()))
			}
			if size := proto.Size(snap); size > runs*4096 {
				t.Fatalf("a snapshot of %d runs is %d bytes: it must not grow with mailboxes %v", runs, size, lengths)
			}
			for _, id := range ids {
				if !slices.ContainsFunc(snap.GetSummaries(), func(s *pb.RunSummary) bool { return s.GetRunId() == string(id) }) {
					t.Fatalf("run %s has no summary", id)
				}
			}
			if err := wire.Validate(snap); err != nil {
				t.Fatal(err)
			}

			// Whole histories: every run is still listed; the history that did not fit is said to be cut.
			_, whole, err := rig.Service.WatchRunsFrom(core.Operator{}, 0)
			if err != nil {
				t.Fatal(err)
			}
			full := client.WatchRunsSnapshot(whole[0])
			held := len(full.GetMessages())
			total := 0
			for _, id := range ids {
				msgs, err := rig.Store.Messages(id, 0, store.MaxPageMessages)
				if err != nil {
					t.Fatal(err)
				}
				total += len(msgs)
			}
			if len(full.GetRuns())+int(full.GetRunsLeftOut()) != runs || (held < total) && !full.GetTruncated() {
				t.Fatalf("%d runs: %d listed and %d left out; %d of the %d messages held, truncated %t", runs, len(full.GetRuns()), full.GetRunsLeftOut(), held, total, full.GetTruncated())
			}
			if size := proto.Size(full); size > daemon.MaxSnapshotBytes {
				t.Fatalf("a snapshot of %d bytes passes the budget of %d", size, daemon.MaxSnapshotBytes)
			}
		})
	}
}

// When even the runs and their summaries do not fit the byte budget, the ones left out are the oldest
// finished runs, they are counted, and nothing is sent about them as though it were listed.
func TestRunsThatDoNotFitAreCountedAndTheOldestGoFirst(t *testing.T) {
	rig := daemontest.NewRig(t)
	ids := startRuns(t, rig, 14, func(int) int { return 0 }, 0)
	// A report of 150 KB for each run makes its summary heavy.
	for i, id := range ids {
		lead := rig.Daemon.State().Agents(id)[0].ID
		post(t, rig, lead, fmt.Sprintf("big-%d", i), &pb.Command{Body: &pb.Command_PostReport{PostReport: &pb.PostReport{Report: &pb.Report{
			Kind: pb.ReportKind_REPORT_KIND_PROGRESS, Headline: "h", Changed: strings.Repeat("c", 150<<10),
		}}}})
	}
	// The oldest runs are over; the newest are live.
	for _, id := range ids[:6] {
		if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: "stop-" + string(id), Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(id)}}}}); err != nil {
			t.Fatal(err)
		}
		rig.Settle(t)
	}
	w, frames, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	snap := client.WatchRunsSnapshot(frames[0])
	if snap.GetRunsLeftOut() == 0 || !snap.GetTruncated() || len(snap.GetRuns())+int(snap.GetRunsLeftOut()) != len(ids) {
		t.Fatalf("%d runs of 150 KB summaries: %d listed, %d left out, truncated %t", len(ids), len(snap.GetRuns()), snap.GetRunsLeftOut(), snap.GetTruncated())
	}
	if size := proto.Size(snap); size > daemon.MaxSnapshotBytes {
		t.Fatalf("%d bytes passes the budget", size)
	}
	listed := map[string]bool{}
	for _, r := range snap.GetRuns() {
		listed[r.GetId()] = true
	}
	// Live runs come before finished ones, and newer before older: what is missing is a suffix of that order.
	var order []string
	for _, id := range slices.Backward(ids[6:]) {
		order = append(order, string(id))
	}
	for _, id := range slices.Backward(ids[:6]) {
		order = append(order, string(id))
	}
	seenMissing := false
	for _, id := range order {
		if !listed[id] {
			seenMissing = true
		} else if seenMissing {
			t.Fatalf("run %s is listed after one that was left out: listed %v of %v", id, listed, order)
		}
	}
	// Nothing about a run left out is sent as a summary, now or when it changes.
	rig.Clock.Advance(time.Minute)
	oldest := ids[0]
	if _, err := press(t, viewing(ann), rig, oldest); err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	polled, err := w.Poll()
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range summariesIn(polled) {
		if !listed[s.GetRunId()] {
			t.Fatalf("a summary for run %s, which the snapshot left out", s.GetRunId())
		}
	}
}

// A run's history is read a page at a time; the pages hold every message once, in the order they were
// made, and only the first page holds the agents, turns and check-ins.
func TestARunsHistoryIsReadInPagesThatHoldEveryMessageOnce(t *testing.T) {
	rig := daemontest.NewRig(t)
	ids := startRuns(t, rig, 2, func(i int) int { return []int{10, 400}[i] }, 6000)
	other := ids[0]
	run := ids[1]
	want, err := allMessages(rig, run)
	if err != nil || len(want) < 400 {
		t.Fatalf("%d messages, %v", len(want), err)
	}
	var got []string
	var cursor uint64
	pages := 0
	for {
		page, err := rig.Service.GetRunHistory(context.Background(), &pb.GetRunHistoryRequest{RunId: string(run), Cursor: cursor})
		if err != nil {
			t.Fatal(err)
		}
		if pages == 0 && (len(page.GetAgents()) == 0 || len(page.GetTurns()) == 0) {
			t.Fatalf("the first page holds %d agents and %d turns", len(page.GetAgents()), len(page.GetTurns()))
		}
		if pages > 0 && (len(page.GetAgents()) != 0 || len(page.GetTurns()) != 0 || len(page.GetCheckIns()) != 0) {
			t.Fatalf("page %d repeats what the first holds", pages)
		}
		bytes := 0
		for _, m := range page.GetMessages() {
			got = append(got, m.GetId())
			bytes += proto.Size(m)
			if m.GetRunId() != string(run) {
				t.Fatalf("a message of run %s in the history of %s", m.GetRunId(), run)
			}
		}
		if len(page.GetMessages()) > 1 && bytes > daemon.MaxHistoryPageBytes {
			t.Fatalf("page %d holds %d bytes", pages, bytes)
		}
		pages++
		if page.GetNextCursor() == 0 {
			break
		}
		if pages > 1000 {
			t.Fatal("the pages never end")
		}
		cursor = page.GetNextCursor()
	}
	if !slices.Equal(got, want) || pages < 2 {
		t.Fatalf("%d pages hold %d messages, want %d", pages, len(got), len(want))
	}
	if _, err := rig.Service.GetRunHistory(context.Background(), &pb.GetRunHistoryRequest{RunId: "no-such-run"}); err == nil {
		t.Fatal("the history of a run the host does not have was served")
	}
	small, err := rig.Service.GetRunHistory(context.Background(), &pb.GetRunHistoryRequest{RunId: string(other)})
	if err != nil || small.GetNextCursor() != 0 || len(small.GetMessages()) == 0 {
		t.Fatalf("a short history is one page: %v, %v", small, err)
	}
}

func allMessages(rig *daemontest.Rig, run core.RunID) ([]string, error) {
	var out []string
	for after := uint64(0); ; {
		page, err := rig.Store.Messages(run, after, store.MaxPageMessages)
		if err != nil {
			return nil, err
		}
		for _, rec := range page {
			out = append(out, string(rec.Message.ID))
		}
		if len(page) < store.MaxPageMessages {
			return out, nil
		}
		after = page[len(page)-1].Seq
	}
}

// A run recorded before the core named its summary report still has one: the newest report about the run
// that did not answer a check-in, found once in its messages.
func TestARunRecordedBeforeItsSummaryReportWasNamedStillHasOne(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, lead := beginRun(t, rig)
	holdCatchUps(rig)
	post(t, rig, lead.ID, "first", reportCommand(pb.ReportKind_REPORT_KIND_PROGRESS, "first"))
	rig.Clock.Advance(time.Minute)
	if _, err := press(t, viewing(ann), rig, run.ID); err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	post(t, rig, lead.ID, "answer", reportCommand(pb.ReportKind_REPORT_KIND_PROGRESS, "a window"))
	whole := post(t, rig, lead.ID, "whole", reportCommand(pb.ReportKind_REPORT_KIND_PROGRESS, "the run, newest"))
	// Rewrite the run as an earlier build stored it: no summary report named, no scope on any report.
	current, _ := rig.Daemon.State().Run(run.ID)
	current.SummaryReport = ""
	event := core.Event{Run: run.ID, At: rig.Clock.Now(), Body: current}
	if _, err := rig.Store.Commit(store.Result{Events: []core.Event{event}}); err != nil {
		t.Fatal(err)
	}
	rig.Restart(t)
	_, frames, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	if got := client.WatchRunsSnapshot(frames[0]).GetSummaries()[0].GetReport().GetId(); got != whole.GetMessageId() {
		t.Fatalf("the summary of a run that names none is %s, want the newest report about the run, %s", got, whole.GetMessageId())
	}
}
