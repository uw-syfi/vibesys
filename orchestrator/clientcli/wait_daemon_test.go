package clientcli_test

import (
	"context"
	"testing"
)

// Against a whole daemon: a run that is going has started, a run its lead has reported has a report,
// and a run that is stopped ends, each without a timer firing.
func TestWaitOnARealDaemonFollowsARunToItsEnd(t *testing.T) {
	l := newLive(t, nil)
	run, lead := l.startRun(t, "/work/repo", "alpha")
	w := newWorld(t, l.Listener.Dial)

	if got := w.run(context.Background(), "run", "wait", "--until", "started", "--timeout", "1m"); got.code != 0 {
		t.Fatalf("wait --until started: exit %d, stderr %q", got.code, got.stderr)
	}
	l.postReport(t, lead, "plan 1 won")
	got := w.run(context.Background(), "run", "wait", run, "--until", "report", "--timeout", "1m")
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
