package apptainer_test

import (
	"bytes"
	"context"
	"fmt"
	"math/rand/v2"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

// reset is a call's failure whose ssh connection the login node reset.
var reset = fmt.Errorf("%w (%w)", &core.Failure{Kind: core.FailureUnavailable,
	Message: "cannot reach host-a to run squeue: kex_exchange_identification: read: Connection reset by peer"}, apptainer.ErrConnectionLost)

const (
	outageStarts = "the cluster is unreachable since"
	outageEnds   = "the cluster answers again"
)

// losing makes the calls of op whose turn the pattern marks lose their connection to the cluster,
// as ssh's exit 255 does (one connection reset by the login node), counting from the first call of
// op; every later call answers.
func losing(cluster *clusterfake.Cluster, op string, pattern []bool) {
	var calls atomic.Int64
	cluster.SetFaults(func(got string) error {
		if got != op {
			return nil
		}
		if n := int(calls.Add(1)) - 1; n < len(pattern) && pattern[n] {
			return reset
		}
		return nil
	})
}

// outages is how many outages the calls of a wait that needs answered calls meet under the
// pattern: the runs of lost calls before the last call the wait makes.
func outages(pattern []bool, answered int) int {
	n, in := 0, false
	for _, isLost := range pattern {
		if answered == 0 {
			break
		}
		if isLost && !in {
			n++
		}
		if !isLost {
			answered--
		}
		in = isLost
	}
	return n
}

// Regression: one ssh connection the login node reset while a run's allocation waited in Slurm's
// queue ended the run, and gave up the allocation's place in the queue after hours. Whatever the
// interleaving of lost connections with the answers of the wait for the allocation, of the step that
// holds it, or of the reading of its node, the run waits through them and its command runs in the
// one allocation it asked for. The log says when each outage starts and when it ends, once each,
// not at every lost call.
func TestAWaitOnTheClusterOutlastsLostConnections(t *testing.T) {
	waits := []struct {
		op     string
		queued int // reads that find the allocation queued
		needs  int // the calls of op the run makes when every one answers
	}{{"state", 3, 4}, {"hold", 0, 1}, {"readnode", 0, 1}}
	rng := rand.New(rand.NewPCG(11, 12))
	patterns := [][]bool{{true}, {true, true, true}, {true, false, true, true, false, true}}
	for range 20 {
		p := make([]bool, 1+rng.IntN(12))
		for i := range p {
			p[i] = rng.IntN(2) == 0
		}
		patterns = append(patterns, p)
	}
	for _, wait := range waits {
		for i, pattern := range patterns {
			t.Run(fmt.Sprintf("%s/%d", wait.op, i), func(t *testing.T) {
				w := fakeworld.New()
				cluster := clusterfake.New(w.MemoryGates)
				cluster.Queue(wait.queued, false)
				r := newRig(t, w, cluster, setup{engineTimers: clock.Immediate{Longest: apptainer.DefaultPoll}})
				losing(cluster, wait.op, pattern)
				a, out := run(t, r.env, "print", "hello")
				exited(t, a, 0)
				want := outages(pattern, wait.needs)
				if got := strings.Count(out, outageStarts); got != want {
					t.Errorf("pattern %v: the log says %d outages start, want %d:\n%s", pattern, got, want, out)
				}
				if got := strings.Count(out, outageEnds); got != want {
					t.Errorf("pattern %v: the log says %d outages end, want %d:\n%s", pattern, got, want, out)
				}
				if n := len(cluster.Submitted()); n != 1 {
					t.Errorf("%d allocations, want the one the run waited for", n)
				}
				if !strings.HasSuffix(out, "hello\n") {
					t.Errorf("the command's output is not last: %q", out)
				}
				nothingLeft(t, cluster)
			})
		}
	}
}

// firing is the engine's timers for a cluster that stays unreachable: the bound on an outage, and
// the bound of a call such as a cancel, fire at once, a poll fires at once, and the queue's bound
// never does.
var firing = clock.Exactly{D: apptainer.DefaultUnreachableTimeout, Others: clock.Exactly{
	D: apptainer.CallTimeout, Others: clock.Immediate{Longest: apptainer.DefaultPoll},
}}

// A cluster unreachable for longer than the bound fails the run before its command, saying so
// with the last failure. The allocation is cancelled once the cluster answers; one the cluster
// cannot be reached to cancel stays known to Slurm by its scope, and the next run cancels it.
func TestAClusterUnreachablePastTheBoundFailsTheRunSayingSo(t *testing.T) {
	for _, cancels := range []bool{true, false} {
		t.Run(fmt.Sprintf("cancels %v", cancels), func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			cluster.Queue(1_000_000, false)
			r := newRig(t, w, cluster, setup{engineTimers: firing})
			cluster.SetFaults(func(op string) error {
				if op == "state" || (op == "cancel" && !cancels) {
					return reset
				}
				return nil
			})
			a, out := run(t, r.env, "mark", "ran")
			f := failure(t, a)
			for _, want := range []string{"unreachable", "longer than " + apptainer.DefaultUnreachableTimeout.String(), "the wait for the allocation 1001 was given up", "Connection reset by peer"} {
				if !strings.Contains(f.Message, want) {
					t.Errorf("failure %q lacks %q", f.Message, want)
				}
			}
			if w.Marked("ran") {
				t.Error("the command ran")
			}
			if n := strings.Count(out, outageStarts); n != 1 {
				t.Errorf("the log says %d outages start, want one:\n%s", n, out)
			}
			if cancels {
				nothingLeft(t, cluster)
				return
			}
			if live := cluster.Live(); len(live) != 1 {
				t.Fatalf("allocations %v, want the one the cluster could not be reached to cancel", live)
			}
			cluster.SetFaults(nil)
			cluster.Queue(0, false)
			a, _ = run(t, newRig(t, w, cluster, setup{process: 1}).env, "exit", "0")
			exited(t, a, 0)
			nothingLeft(t, cluster)
		})
	}
}

// A run cancelled while the cluster is unreachable ends at once, its command never run and its
// allocation cancelled, and not by the bound on the outage.
func TestARunCancelledDuringAnOutageEnds(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	cluster.Queue(1_000_000, false)
	// The engine's timers never fire: only the cancel ends the wait.
	r := newRig(t, w, cluster, setup{})
	entered := make(chan struct{})
	var once sync.Once
	cluster.SetFaults(func(op string) error {
		if op != "state" {
			return nil
		}
		once.Do(func() { close(entered) })
		return reset
	})
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	runCtx, stop := context.WithCancel(ctx)
	var out bytes.Buffer
	done := make(chan core.Attempt, 1)
	go func() {
		done <- r.env.Run(runCtx, core.Execution{Checkout: checkout(t), Argv: []string{"mark", "ran"}, Timeout: limit}, &out)
	}()
	receive(ctx, t, entered, "the first lost read")
	stop()
	a := receive(ctx, t, done, "the run's end")
	if a != (core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}) {
		t.Fatalf("attempt %+v, want a cancelled run", a)
	}
	if strings.Contains(out.String(), "given up") {
		t.Errorf("log %q: the cancel, not the outage's bound, ended the run", out.String())
	}
	if w.Marked("ran") {
		t.Error("the command ran")
	}
	nothingLeft(t, cluster)
}

// A read of the queue that fails otherwise than by its connection, such as a squeue that reports an
// error, fails the run at once, as it did: only an unreachable cluster is waited through.
func TestAReadThatFailsOtherwiseIsNotWaitedThrough(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	cluster.Queue(3, false)
	r := newRig(t, w, cluster, setup{engineTimers: clock.Immediate{Longest: apptainer.DefaultPoll}})
	broken := &core.Failure{Kind: core.FailureUnavailable, Message: "squeue failed: slurm_load_jobs error"}
	var reads atomic.Int64
	cluster.SetFaults(func(op string) error {
		if op == "state" {
			reads.Add(1)
			return broken
		}
		return nil
	})
	a, out := run(t, r.env, "mark", "ran")
	if f := failure(t, a); !strings.Contains(f.Message, broken.Message) {
		t.Errorf("failure %q, want %q", f.Message, broken.Message)
	}
	if n := reads.Load(); n != 1 {
		t.Errorf("%d reads, want one", n)
	}
	if strings.Contains(out, outageStarts) {
		t.Errorf("the log says the cluster is unreachable: %q", out)
	}
	cluster.SetFaults(nil)
	nothingLeft(t, cluster)
}

// The check that the allocation a sample kept still runs waits through an unreachable cluster as
// the queue's does: the next sample runs there, and the job does not lose its place on the node.
func TestAKeptAllocationIsCheckedThroughAnOutage(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	r := newRig(t, w, cluster, setup{engineTimers: clock.Immediate{Longest: apptainer.DefaultPoll}})
	a, _ := sample(context.Background(), t, r.env, limit, "exit", "0")
	exited(t, a, 0)
	losing(cluster, "state", []bool{true, true, true})
	a, out := sample(context.Background(), t, r.env, 0, "print", "hello")
	exited(t, a, 0)
	if !strings.Contains(out, "which the job's last container kept") {
		t.Errorf("the sample did not run in the kept allocation: %q", out)
	}
	if strings.Count(out, outageStarts) != 1 || strings.Count(out, outageEnds) != 1 {
		t.Errorf("the log does not say once that the cluster was unreachable and once that it answers again:\n%s", out)
	}
	if n := len(cluster.Submitted()); n != 1 {
		t.Errorf("%d allocations, want the kept one only", n)
	}
	nothingLeft(t, cluster)
}
