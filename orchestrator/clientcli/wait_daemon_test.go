package clientcli_test

import (
	"context"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Against a whole daemon: a final report is told from a progress note by the daemon's own numbering,
// and a lead that rests with nothing outstanding is a stalled run. The sidecars of the rig end
// every turn at once, so the run stands still as soon as its lead has had its brief.
func TestWaitOnARealDaemonTellsReportsApartAndSeesAStall(t *testing.T) {
	l := newLive(t, nil)
	run, lead := l.startRun(t, "/work/repo", "alpha")
	w := newWorld(t, l.Listener.Dial)

	if got := w.run(context.Background(), "run", "wait", run, "--until", "stalled", "--timeout", "1m"); got.code != 0 {
		t.Fatalf("wait --until stalled: exit %d, stderr %q", got.code, got.stderr)
	}
	l.postReportOf(t, lead, "halfway", pb.ReportKind_REPORT_KIND_PROGRESS)
	got := w.run(context.Background(), "run", "wait", run, "--until", "progress", "--after", "0", "--timeout", "1m")
	if got.code != 0 {
		t.Fatalf("wait --until progress --after 0: exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "progress report 1", "halfway")
	l.postReportOf(t, lead, "done", pb.ReportKind_REPORT_KIND_FINAL)
	got = w.run(context.Background(), "run", "wait", run, "--until", "report", "--after", "1", "--timeout", "1m")
	if got.code != 0 {
		t.Fatalf("wait --until report --after 1: exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "final report 2", "done")
	got = w.run(context.Background(), "run", "get", run)
	contains(t, "get", got.stdout, "stalled for")
}

// Against a whole daemon: a run that is going has started, a run its lead has reported has a report,
// and a run that is stopped ends, each without a timer firing.
func TestWaitOnARealDaemonFollowsARunToItsEnd(t *testing.T) {
	l := newLive(t, nil)
	run, lead := l.startRun(t, "/work/repo", "alpha")
	w := newWorld(t, l.Listener.Dial)

	if got := w.run(context.Background(), "run", "wait", "--until", "started", "--timeout", "1m"); got.code != 0 {
		t.Fatalf("wait --until started: exit %d, stderr %q", got.code, got.stderr)
	}
	l.postReportOf(t, lead, "plan 1 won", pb.ReportKind_REPORT_KIND_FINAL)
	got := w.run(context.Background(), "run", "wait", run, "--until", "report", "--after", "0", "--timeout", "1m")
	if got.code != 0 {
		t.Fatalf("wait --until report: exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "plan 1 won")
	if stop := w.run(context.Background(), "run", "stop", "--yes"); stop.code != 0 {
		t.Fatalf("stop: exit %d, stderr %q", stop.code, stop.stderr)
	}
	got = w.run(context.Background(), "run", "wait", run, "--until", "ended", "--timeout", "1m")
	if got.code != 0 {
		t.Fatalf("wait --until ended: exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "ended")
}
