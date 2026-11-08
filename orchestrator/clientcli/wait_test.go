package clientcli_test

import (
	"context"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

func waitArgs(until string) []string {
	return []string{"run", "wait", "run-1", "--until", until, "--timeout", "90s"}
}

func TestWaitReturnsAtOnceWhenTheConditionAlreadyHolds(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	live := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)
	ended := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	final := reportOf("run-1", "m1", "done", 3, 1, pb.ReportKind_REPORT_KIND_FINAL)
	cases := map[string]struct {
		frame *pb.WatchRunsResponse
		extra []string
	}{
		"started": {frame: snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{live})},
		// A final report that came before the wait is the one a late caller gets, with no --after.
		"report":   {frame: snapshotFrame(5, false, []*pb.Run{reported("run-1", 1)}, []*pb.Agent{live}, final)},
		"progress": {frame: snapshotFrame(5, false, []*pb.Run{reported("run-1", 1)}, []*pb.Agent{live}, reportMessage("run-1", "m1", "note", 3)), extra: []string{"--after", "0"}},
		"stalled":  {frame: snapshotFrame(5, false, []*pb.Run{stalledRun("run-1", 1)}, []*pb.Agent{live})},
		"ended":    {frame: snapshotFrame(5, false, []*pb.Run{ended}, []*pb.Agent{live})},
	}
	for until, c := range cases {
		t.Run(until, func(t *testing.T) {
			w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{c.frame}}))
			got := w.run(context.Background(), append(waitArgs(until), c.extra...)...)
			if got.code != 0 {
				t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
			}
			contains(t, "stdout", got.stdout, "run-1")
		})
	}
}

func TestWaitSubscribesAndReturnsWhenTheEventArrives(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	launching := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_LAUNCHING)
	launching.Capabilities = nil
	live := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)
	f := &fakeService{frames: []*pb.WatchRunsResponse{
		snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{launching}),
		agentEvent(6, live, 1),
	}}
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), waitArgs("started")...)
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "is live")
}

func TestWaitOnARunThatEndsFirstExitsOne(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	ended := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	ended.StopReason = pb.RunStopReason_RUN_STOP_REASON_OPERATOR
	f := &fakeService{frames: []*pb.WatchRunsResponse{
		snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)}),
		runEvent(6, ended, 2),
	}}
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), waitArgs("report")...)
	if got.code != 1 {
		t.Fatalf("exit %d, want 1; stderr %q", got.code, got.stderr)
	}
	contains(t, "stderr", got.stderr, "ended by operator before a final report was posted")
}

func TestWaitForTheEndOfARunThatFailedExitsOne(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_FAILED, 0)
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_FAILED)
	lead.EndDetail = "no credentials"
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{lead})}}))
	got := w.run(context.Background(), waitArgs("ended")...)
	if got.code != 1 {
		t.Fatalf("exit %d, want 1", got.code)
	}
	contains(t, "stderr", got.stderr, "no credentials")
}

// The timeout is a bound on the wait, driven by the clock and not by the stream: nothing arrives, the
// clock fires, and the verb exits 3 whether or not it had read the snapshot yet.
func TestWaitTimesOutWithExitThree(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})}}
	w := newWorld(t, serveFake(t, f))
	got := w.runWaitingToTimeOut(waitArgs("report")...)
	if got.code != 3 {
		t.Fatalf("exit %d, want 3; stderr %q", got.code, got.stderr)
	}
	contains(t, "stderr", got.stderr, "timed out after 1m30s", "a final report was posted", "run-1 is running")
	if strings.Contains(got.stderr, "stalled") {
		t.Errorf("a run that is not stalled is called stalled: %q", got.stderr)
	}
}

func TestWaitJSONIsTheSnapshotOfTheRun(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	run.ReportsPosted = 1
	frame := snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)}, reportOf("run-1", "m1", "done", 3, 1, pb.ReportKind_REPORT_KIND_FINAL))
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	got := w.run(context.Background(), append(waitArgs("report"), "--after", "0", "--json")...)
	var snapshot pb.RunsSnapshot
	if err := protojson.Unmarshal([]byte(got.stdout), &snapshot); err != nil || len(snapshot.GetMessages()) != 1 {
		t.Fatalf("stdout %q: %v", got.stdout, err)
	}
}

func TestWaitNeedsAGoalAndABound(t *testing.T) {
	w := newWorld(t, serveFake(t, &fakeService{}))
	for _, args := range [][]string{
		{"run", "wait", "--timeout", "1m"},
		{"run", "wait", "--until", "report"},
		{"run", "wait", "--until", "later", "--timeout", "1m"},
		{"run", "wait", "--until", "report", "--timeout", "0s"},
	} {
		if got := w.run(context.Background(), args...); got.code != 2 {
			t.Errorf("%v: exit %d, want 2", args, got.code)
		}
	}
}

// A progress note is not the result. A wait for the final report returns at once with the one a
// running run already has, however late the caller joins (the live finding: it used to run to its
// timeout unless --after 0 was passed), and --after N keeps its meaning: it waits for a final report
// numbered above N, ignoring progress notes and the final report it has already read.
func TestWaitForAReportReturnsTheFinalReportARunAlreadyHasAndAfterKeepsItsMeaning(t *testing.T) {
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)
	earlier := []*pb.Message{
		reportOf("run-1", "m1", "first note", 1, 1, pb.ReportKind_REPORT_KIND_PROGRESS),
		reportOf("run-1", "m2", "an early final", 2, 2, pb.ReportKind_REPORT_KIND_FINAL),
	}
	frames := []*pb.WatchRunsResponse{
		snapshotFrame(5, false, []*pb.Run{reported("run-1", 2)}, []*pb.Agent{lead}, earlier...),
		runEvent(6, reported("run-1", 3), 4),
		messageEvent(7, reportOf("run-1", "m3", "a new note", 4, 3, pb.ReportKind_REPORT_KIND_PROGRESS), 4),
		runEvent(8, reported("run-1", 4), 5),
		messageEvent(9, reportOf("run-1", "m4", "the result", 5, 4, pb.ReportKind_REPORT_KIND_FINAL), 5),
	}
	w := newWorld(t, serveFake(t, &fakeService{frames: frames}))
	got := w.run(context.Background(), waitArgs("report")...)
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "final report 2", "an early final")

	got = w.run(context.Background(), append(waitArgs("report"), "--after", "2")...)
	if got.code != 0 {
		t.Fatalf("--after 2: exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "final report 4", "the result")

	// Any report is enough for progress, and the first one after the wait began is the one it reports.
	got = w.run(context.Background(), waitArgs("progress")...)
	if got.code != 0 {
		t.Fatalf("progress: exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "progress report 3", "a new note")
}

// --after names the last report the caller has read, whatever its kind.
func TestWaitAfterCountsTheReportsNumberedAboveIt(t *testing.T) {
	messages := []*pb.Message{
		reportOf("run-1", "m1", "one", 1, 1, pb.ReportKind_REPORT_KIND_PROGRESS),
		reportOf("run-1", "m2", "two", 2, 2, pb.ReportKind_REPORT_KIND_PROGRESS),
	}
	frame := snapshotFrame(5, false, []*pb.Run{reported("run-1", 2)}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)}, messages...)
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	got := w.run(context.Background(), append(waitArgs("progress"), "--after", "1")...)
	if got.code != 0 {
		t.Fatalf("--after 1: exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "report 2", "two")

	w = newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	if got := w.runWaitingToTimeOut(append(waitArgs("progress"), "--after", "2")...); got.code != 3 {
		t.Fatalf("--after 2: exit %d, want 3; stderr %q", got.code, got.stderr)
	}
}

// A run that has ended posts no more reports, so the wait returns the one it has.
func TestWaitForAReportOnAnEndedRunReturnsTheFinalReportItHas(t *testing.T) {
	ended := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	ended.ReportsPosted = 2
	messages := []*pb.Message{
		reportOf("run-1", "m1", "the result", 1, 1, pb.ReportKind_REPORT_KIND_FINAL),
		reportOf("run-1", "m2", "after it", 2, 2, pb.ReportKind_REPORT_KIND_PROGRESS),
	}
	frame := snapshotFrame(5, false, []*pb.Run{ended}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ENDED)}, messages...)
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	got := w.run(context.Background(), waitArgs("report")...)
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "final report 1", "the result")
}

// A wait for a result from a run that stands still does not return, and its timeout says why.
func TestWaitForAReportTimesOutAndSaysTheRunIsStalled(t *testing.T) {
	frame := snapshotFrame(5, false, []*pb.Run{stalledRun("run-1", 0)}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	got := w.runWaitingToTimeOut(waitArgs("report")...)
	if got.code != 3 {
		t.Fatalf("exit %d, want 3; stderr %q", got.code, got.stderr)
	}
	contains(t, "stderr", got.stderr, "timed out after 1m30s", "run-1 is running, stalled for 4m")
}

// The wait for a stall subscribes: nothing is read until the run stands still.
func TestWaitUntilStalledReturnsWhenTheRunStandsStill(t *testing.T) {
	frames := []*pb.WatchRunsResponse{
		snapshotFrame(5, false, []*pb.Run{runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)}),
		runEvent(6, stalledRun("run-1", 3), 3),
	}
	w := newWorld(t, serveFake(t, &fakeService{frames: frames}))
	got := w.run(context.Background(), waitArgs("stalled")...)
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "run-1 is stalled", "idle since 03:03:00")
}

func TestWaitAfterGoesOnlyWithReports(t *testing.T) {
	w := newWorld(t, serveFake(t, &fakeService{}))
	got := w.run(context.Background(), append(waitArgs("ended"), "--after", "1")...)
	if got.code != 2 {
		t.Fatalf("exit %d, want 2", got.code)
	}
	contains(t, "stderr", got.stderr, "--after")
}

// --after names a report number, and reports are numbered from 1 (or 0 to count every report): a
// negative number names none, so it is a usage error, the same as --after on a goal that is not a
// report. -1 itself stays reserved to mean "--after was not given" and is covered by the default case.
func TestWaitAfterRefusesANegativeNumber(t *testing.T) {
	w := newWorld(t, serveFake(t, &fakeService{}))
	got := w.run(context.Background(), append(waitArgs("report"), "--after", "-3")...)
	if got.code != 2 {
		t.Fatalf("exit %d, want 2", got.code)
	}
	contains(t, "stderr", got.stderr, "--after", "-3")
}

// A report number is a uint32, and --after is cast down to one. A value past uint32 would wrap --
// 4294967296 to 0 -- and so be treated as "count every report" instead of a number no report can
// reach, silently ending a wait that should never complete. It is refused as a usage error naming
// the value, the same as a negative one, and nothing is waited on.
func TestWaitAfterRefusesANumberTooLargeToBeAReportNumber(t *testing.T) {
	messages := []*pb.Message{
		reportOf("run-1", "m1", "one", 1, 1, pb.ReportKind_REPORT_KIND_PROGRESS),
		reportOf("run-1", "m2", "two", 2, 2, pb.ReportKind_REPORT_KIND_PROGRESS),
	}
	frame := snapshotFrame(5, false, []*pb.Run{reported("run-1", 2)}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)}, messages...)
	// 4294967296 is 1<<32: cast to uint32 it is 0, which would make a wait for a report above it return
	// the newest report at once instead of waiting. 4294967297 would wrap to 1, and so on.
	for _, value := range []string{"4294967296", "4294967297", "9999999999"} {
		w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
		got := w.run(context.Background(), append(waitArgs("progress"), "--after", value)...)
		if got.code != 2 {
			t.Fatalf("--after %s: exit %d, want 2; stderr %q", value, got.code, got.stderr)
		}
		contains(t, "stderr", got.stderr, "--after", value)
	}
}

// With only progress notes so far there is no final report to return, so a wait for one waits.
func TestWaitForAReportWaitsWhenARunHasOnlyProgressNotes(t *testing.T) {
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)
	note := reportOf("run-1", "m1", "a note", 1, 1, pb.ReportKind_REPORT_KIND_PROGRESS)
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{reported("run-1", 1)}, []*pb.Agent{lead}, note)}}))
	if got := w.runWaitingToTimeOut(waitArgs("report")...); got.code != 3 {
		t.Fatalf("exit %d, want 3; stderr %q", got.code, got.stderr)
	}
}

// A lead that gave its final report and then answered a question in text has not stalled, and a wait
// for a later final report does not say it has: it times out saying the lead's report is final and it
// started nothing since, which is how a person learns the answer is in the lead's reply.
func TestWaitAfterAFinalReportTimesOutWithoutCallingTheRunStalled(t *testing.T) {
	run := reported("run-1", 1)
	run.FinalReported = true
	final := reportOf("run-1", "m1", "the result", 1, 1, pb.ReportKind_REPORT_KIND_FINAL)
	frame := snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)}, final)
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	got := w.runWaitingToTimeOut(append(waitArgs("report"), "--after", "1")...)
	if got.code != 3 {
		t.Fatalf("exit %d, want 3; stderr %q", got.code, got.stderr)
	}
	contains(t, "stderr", got.stderr, "run-1 is running, its lead has given its final report", "number 1, is final", "answered in text")
	if strings.Contains(got.stderr, "stalled") {
		t.Errorf("a lead that finished is called stalled: %q", got.stderr)
	}

	// Without --after the same wait returns at once with that final report.
	w = newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	if got := w.run(context.Background(), waitArgs("report")...); got.code != 0 {
		t.Fatalf("without --after: exit %d, stderr %q", got.code, got.stderr)
	}
}

// No wait hangs past the end of a run: one for a report on a run its limit stopped returns at once,
// failed, naming the limit; one for its end returns normally.
func TestWaitsOnARunStoppedByItsLimitReturnAndNameTheLimit(t *testing.T) {
	for _, reason := range []pb.RunStopReason{pb.RunStopReason_RUN_STOP_REASON_TIME_LIMIT, pb.RunStopReason_RUN_STOP_REASON_COST_LIMIT} {
		ended := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
		ended.StopReason = reason
		frame := snapshotFrame(5, false, []*pb.Run{ended}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ENDED)})
		words := strings.ToLower(strings.ReplaceAll(strings.TrimPrefix(reason.String(), "RUN_STOP_REASON_"), "_", " "))

		w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
		got := w.run(context.Background(), waitArgs("report")...)
		if got.code != 1 {
			t.Fatalf("%s: wait for a report exited %d, want 1; stderr %q", reason, got.code, got.stderr)
		}
		contains(t, "stderr", got.stderr, "ended by "+words+" before a final report was posted")

		w = newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
		got = w.run(context.Background(), waitArgs("ended")...)
		if got.code != 0 {
			t.Fatalf("%s: wait for the end exited %d, stderr %q", reason, got.code, got.stderr)
		}
		contains(t, "stdout", got.stdout, "ended by "+words)
	}
}
