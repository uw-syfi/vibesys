package lifecycle_test

import (
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

// These tests hold the retry rule, one row at a time: what comes of a job whose run ended before it
// could report, by what is proved of the run and by the kind of job.

var (
	policies = []core.RerunPolicy{core.RerunBeforeStartOnly, core.RerunAfterProvenDeath}
	proofs   = []lifecycle.DeathProof{lifecycle.ProofLockFree, lifecycle.ProofRebooted, lifecycle.ProofReleased, ""}
	starts   = []lifecycle.Start{lifecycle.NeverStarted{}, lifecycle.MayHaveStarted{}, nil}

	noSupervisor = core.Failure{Kind: core.FailureUnavailable, Message: "the supervisor never came up"}
)

// CutOff queues a job again exactly when its supervisor is proved dead, its command never started
// or its kind may run again after a proven death, and it has an attempt left. Whatever it decides,
// the store takes it from a job with that many attempts.
func TestCutOffQueuesAJobAgainOnlyWithProofAndAnAttemptLeft(t *testing.T) {
	for _, policy := range policies {
		for attempts := range core.MaxAttempts {
			for _, proof := range proofs {
				for _, start := range starts {
					st := lifecycle.CutOff("j", policy, attempts, proof, start, epoch)
					_, never := start.(lifecycle.NeverStarted)
					dead := proof == lifecycle.ProofLockFree || proof == lifecycle.ProofRebooted
					want := dead && (never || policy == core.RerunAfterProvenDeath) && attempts+1 < core.MaxAttempts
					name := strings.Join([]string{string(policy), string(proof)}, ", ")
					if st.Requeues() != want {
						t.Errorf("%s, %d attempts, start %T: requeues = %v, want %v (%+v)", name, attempts, start, st.Requeues(), want, st)
					}
					if !want && st.Transition.To != core.JobLost {
						t.Errorf("%s, %d attempts, start %T: ends %q, want lost", name, attempts, start, st.Transition.To)
					}
					if want {
						_, before := st.Transition.Cut.(core.CutBeforeStart)
						if before != never {
							t.Errorf("%s, start %T: cut %#v", name, start, st.Transition.Cut)
						}
					}
					job := core.Job{
						ID: "j", State: core.JobRunning, Spec: core.JobSpec{Settings: core.CommandSettings{}},
						Attempts: make([]core.PastAttempt, attempts),
					}
					if _, err := job.Apply(st.Transition); err != nil {
						t.Errorf("%s, %d attempts, start %T: the store refuses %+v: %v", name, attempts, start, st.Transition, err)
					}
					if st.Log == "" {
						t.Errorf("%s, %d attempts, start %T: no log line says why", name, attempts, start)
					}
				}
			}
		}
	}
}

// driver steps one job through the machine by hand and checks each request it makes.
type driver struct {
	t     *testing.T
	state lifecycle.State
	now   time.Time
}

func newDriver(t *testing.T, attempts int, policy core.RerunPolicy) *driver {
	t.Helper()
	d := &driver{t: t, now: epoch}
	d.want(lifecycle.Admitted{Ref: aRef, SubmittedAt: epoch, Attempts: attempts, Policy: policy}, "LaunchRequest")
	return d
}

func (d *driver) at() time.Time {
	d.now = d.now.Add(time.Second)
	return d.now
}

// want feeds the event and checks the machine asks for exactly the named requests.
func (d *driver) want(e lifecycle.Event, wantKinds ...string) []lifecycle.Request {
	d.t.Helper()
	var reqs []lifecycle.Request
	d.state, reqs = lifecycle.Step(d.state, e)
	if got := kinds(reqs); !slices.Equal(got, wantKinds) {
		d.t.Fatalf("after %T: requests = %v, want %v", e, got, wantKinds)
	}
	return reqs
}

// launch takes the queued job to running, its supervisor adopted.
func (d *driver) launch() {
	d.t.Helper()
	d.want(lifecycle.Launched{Ref: aRef}, "Adopt")
}

// move checks the one request is a Move and returns it.
func (d *driver) move(reqs []lifecycle.Request) lifecycle.Move {
	d.t.Helper()
	m, ok := reqs[0].(lifecycle.Move)
	if !ok {
		d.t.Fatalf("request %#v is not a Move", reqs[0])
	}
	return m
}

// requeue answers a Move that queues the job again, and checks the dead run is released before the
// job is launched again.
func (d *driver) requeue(m lifecycle.Move) {
	d.t.Helper()
	if !m.Settlement.Requeues() || m.Release {
		d.t.Fatalf("move %+v: want a requeue that keeps the run until the store has it queued", m)
	}
	reqs := d.want(lifecycle.Requeued{Ref: aRef}, "Discard")
	if r := reqs[0].(lifecycle.Discard); !r.Queued {
		d.t.Fatalf("discard %+v: want one a queued job waits for", r)
	}
	d.want(lifecycle.Discarded{Ref: aRef, At: d.at()}, "LaunchRequest")
}

// A launch whose supervisor never came up leaves the job queued with nothing of it run, so it is
// tried again, and fails once it used all its attempts.
func TestALaunchWhoseSupervisorNeverCameUpIsTriedAgainUpToTheBound(t *testing.T) {
	for _, policy := range policies {
		d := newDriver(t, 0, policy)
		for range core.MaxAttempts - 1 {
			d.want(lifecycle.LaunchFailed{Ref: aRef, Cause: lifecycle.CauseSupervisorSilent, Failure: noSupervisor, At: d.at()}, "Discard")
			d.want(lifecycle.Discarded{Ref: aRef, At: d.at()}, "LaunchRequest")
		}
		m := d.move(d.want(lifecycle.LaunchFailed{Ref: aRef, Cause: lifecycle.CauseSupervisorSilent, Failure: noSupervisor, At: d.at()}, "Move"))
		if m.Settlement.Transition.To != core.JobFailed {
			t.Errorf("%s: after %d launches the job ends %q, want failed", policy, core.MaxAttempts, m.Settlement.Transition.To)
		}
	}
}

// A supervisor proved dead before its command started is run again, whatever the kind of job, as
// long as the job has attempts left; the last attempt cut off loses the job.
func TestASupervisorDeadBeforeItsCommandStartedRunsTheJobAgain(t *testing.T) {
	for _, policy := range policies {
		for _, proof := range []lifecycle.DeathProof{lifecycle.ProofLockFree, lifecycle.ProofRebooted} {
			d := newDriver(t, 0, policy)
			for attempt := 1; ; attempt++ {
				d.launch()
				m := d.move(d.want(lifecycle.SupervisorLost{Ref: aRef, Proof: proof, Start: lifecycle.NeverStarted{}, At: d.at()}, "Move"))
				if attempt == core.MaxAttempts {
					if m.Settlement.Transition.To != core.JobLost || !m.Release {
						t.Errorf("%s, %s: the last attempt ends %+v, want lost and released", policy, proof, m)
					}
					break
				}
				if _, ok := m.Settlement.Transition.Cut.(core.CutBeforeStart); !ok {
					t.Fatalf("%s, %s: attempt %d: cut %#v, want before start", policy, proof, attempt, m.Settlement.Transition.Cut)
				}
				d.requeue(m)
			}
		}
	}
}

// A supervisor proved dead after its command may have started runs a check or a measure job again,
// up to the bound, and never a command job.
func TestAJobCutOffMidCommandRunsAgainOnlyIfItsKindAllows(t *testing.T) {
	for _, start := range []lifecycle.Start{lifecycle.MayHaveStarted{}, nil} {
		d := newDriver(t, 0, core.RerunBeforeStartOnly)
		d.launch()
		m := d.move(d.want(lifecycle.SupervisorLost{Ref: aRef, Proof: lifecycle.ProofLockFree, Start: start, At: d.at()}, "Move"))
		if m.Settlement.Transition.To != core.JobLost {
			t.Errorf("start %T: a command job cut off mid-command ends %q, want lost", start, m.Settlement.Transition.To)
		}

		d = newDriver(t, 0, core.RerunAfterProvenDeath)
		for attempt := 1; ; attempt++ {
			d.launch()
			m := d.move(d.want(lifecycle.SupervisorLost{Ref: aRef, Proof: lifecycle.ProofRebooted, Start: start, At: d.at()}, "Move"))
			if attempt == core.MaxAttempts {
				if m.Settlement.Transition.To != core.JobLost {
					t.Errorf("start %T: the last attempt of a check ends %q, want lost", start, m.Settlement.Transition.To)
				}
				break
			}
			if _, ok := m.Settlement.Transition.Cut.(core.CutMidCommand); !ok {
				t.Fatalf("start %T: attempt %d: cut %#v, want mid-command", start, attempt, m.Settlement.Transition.Cut)
			}
			d.requeue(m)
		}
	}
}

// A job whose command ran and failed, or ran out of time, ends there: an outcome is never a reason
// to run the job again.
func TestAJobThatRanAndFailedIsNeverRunAgain(t *testing.T) {
	ends := []core.ExecOutcome{
		{Kind: core.ExecExited, ExitCode: 1}, {Kind: core.ExecTimedOut}, {Kind: core.ExecCancelled},
	}
	for _, policy := range policies {
		for _, exec := range ends {
			d := newDriver(t, 0, policy)
			d.launch()
			m := d.move(d.want(lifecycle.Finished{Ref: aRef, At: d.at(), Outcome: lifecycle.Outcome{
				End: lifecycle.CommandEnded{Attempt: core.Ended{Exec: exec}},
			}}, "Move"))
			if m.Settlement.Requeues() || !m.Settlement.Transition.To.Final() {
				t.Errorf("%s, %+v: the job moves %+v, want a final state", policy, exec, m.Settlement.Transition)
			}
		}
	}
}

// A supervisor that cannot be judged dead proves nothing, so the job is not settled: it waits for a
// look, held as a job whose command may run.
func TestASupervisorThatCannotBeJudgedLeavesTheJobWaiting(t *testing.T) {
	for _, policy := range policies {
		for _, start := range starts {
			d := newDriver(t, 0, policy)
			d.launch()
			d.want(lifecycle.SupervisorLost{Ref: aRef, Start: start, At: d.at()}, "Reexamine")
			d.want(lifecycle.Recovered{
				Ref: aRef, Finding: lifecycle.JobIn{State: core.JobRunning, SubmittedAt: epoch, Policy: policy},
				Supervisor: lifecycle.RunUnknown{Limit: time.Hour}, At: d.at(),
			}, "ArmRetry")
			if held := d.state.Held(); len(held) != 1 || held[0].Phase != lifecycle.PhaseInDoubt {
				t.Errorf("%s, start %T: held %+v, want the job in doubt", policy, start, held)
			}
		}
	}
}

// A restart that finds a running job whose run is proved dead decides by the same rule, with the
// attempts its store records.
func TestARestartDecidesADeadRunByTheSameRule(t *testing.T) {
	for _, policy := range policies {
		for attempts := range core.MaxAttempts {
			for _, start := range starts {
				s, reqs := lifecycle.Step(lifecycle.Recovering(), lifecycle.Recovered{
					Ref:        aRef,
					Finding:    lifecycle.JobIn{State: core.JobRunning, SubmittedAt: epoch, Attempts: attempts, Policy: policy},
					Supervisor: lifecycle.RunDead{Proof: lifecycle.ProofLockFree, Start: start},
					At:         epoch,
				})
				_, more := lifecycle.Step(s, lifecycle.RecoveryDone{})
				reqs = append(reqs, more...)
				if got := kinds(reqs); !slices.Equal(got, []string{"Move"}) {
					t.Fatalf("requests = %v, want a Move", got)
				}
				m := reqs[0].(lifecycle.Move)
				want := lifecycle.CutOff(aRef.Job, policy, attempts, lifecycle.ProofLockFree, start, epoch)
				if m.Settlement.Transition.To != want.Transition.To || m.Settlement.Requeues() != want.Requeues() {
					t.Errorf("%s, %d attempts, start %T: move %+v, want %+v", policy, attempts, start, m.Settlement, want)
				}
			}
		}
	}
}

// A job the user asked to cancel is never run again: cut off, it is lost, and a cancel that comes
// while its new attempt is recorded ends it cancelled before it is launched.
func TestACancelledJobIsNeverRunAgain(t *testing.T) {
	d := newDriver(t, 0, core.RerunAfterProvenDeath)
	d.launch()
	d.want(lifecycle.CancelAsked{Ref: aRef, Reason: "r", State: core.JobRunning, SubmittedAt: epoch, At: d.at()}, "SignalCancel")
	m := d.move(d.want(lifecycle.SupervisorLost{Ref: aRef, Proof: lifecycle.ProofLockFree, Start: lifecycle.NeverStarted{}, At: d.at()}, "Move"))
	if m.Settlement.Requeues() {
		t.Errorf("a job with a cancel asked is queued again: %+v", m.Settlement)
	}

	d = newDriver(t, 0, core.RerunAfterProvenDeath)
	d.launch()
	m = d.move(d.want(lifecycle.SupervisorLost{Ref: aRef, Proof: lifecycle.ProofLockFree, Start: lifecycle.NeverStarted{}, At: d.at()}, "Move"))
	d.want(lifecycle.CancelAsked{Ref: aRef, Reason: "r", State: core.JobRunning, SubmittedAt: epoch, At: d.at()})
	if !m.Settlement.Requeues() {
		t.Fatalf("want a requeue, got %+v", m.Settlement)
	}
	d.want(lifecycle.Requeued{Ref: aRef}, "Discard")
	end := d.move(d.want(lifecycle.Discarded{Ref: aRef, At: d.at()}, "Move"))
	if end.Settlement.Transition.To != core.JobCancelled {
		t.Errorf("a job cancelled while queued again ends %+v, want cancelled", end.Settlement.Transition)
	}
}
