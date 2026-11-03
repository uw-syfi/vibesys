//go:build !race

// This file is a sweep: it walks a large space of cases on one goroutine, so the race detector has
// nothing to check and only slows it down. check.sh runs it in its step without the detector, and
// archcheck holds every file here to that: named *_sweep_test.go, built only without -race, and
// free of goroutines.

package lifecycle_test

import (
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

// The model check walks every order of events the shell can produce for two jobs, to a depth, from
// the machine's public Step. The model is the world around the machine: the stores, the supervisors
// and the requests in flight, each carried out the way internal/app carries it out. After every
// event it checks what must always hold; from every state it reached, it heals the world and checks
// that every job ends, and with a store gone it checks that the other store's jobs end.

const (
	modelJobs  = 2
	modelLimit = time.Hour
	// modelStates only guards against a search that does not end.
	modelStates = 2_000_000
)

var (
	modelEpoch = time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	modelStop  = core.Failure{Kind: core.FailureUnavailable, Message: "no"}
)

// gone is what a job's store shows once its repository was forgotten: nothing, for good.
const gone core.JobState = "forgotten"

// run is what is left of one job's supervisor.
type run struct {
	phase  lifecycle.RunPhase // Empty when there is no run.
	dead   bool
	cancel bool
	// apart says the run is in a place the daemon cannot see, such as the machine of an owner the
	// store had before: no look finds it, and its command does not use this machine.
	apart bool
	// started says the run's record has the start mark: the supervisor was told, or was about to be
	// told, to start the command.
	started bool
}

func (r run) alive() bool { return r.phase != "" && !r.dead }

// visible says a look can find the run.
func (r run) visible() bool { return r.phase != "" && !r.apart }

// model is the world at one moment.
type model struct {
	machine   lifecycle.State
	jobs      [modelJobs]core.JobState
	runs      [modelJobs]run
	following [modelJobs]bool
	// taken says the machine took a cancel while the job's store had it queued.
	taken [modelJobs]bool
	// died says its supervisor died or went silent, which can decide a job's end before a cancel.
	died [modelJobs]bool
	away [modelJobs]bool // By store.
	// forgetting says the shell is giving a store up: it ended the runs it could, and has not told
	// the machine yet. By store.
	forgetting [modelJobs]bool
	// left is the phase the shell found a run in that it could not end, while it forgets the store.
	left [modelJobs]lifecycle.RunPhase
	// givenUp says the job ran when its store's owner record was removed, which is how a user gives
	// up the place that ran it. By job.
	givenUp [modelJobs]bool
	// attempts is how many attempts of the job its store records as cut off, and ran how many times
	// a command of the job started. By job.
	attempts [modelJobs]int
	ran      [modelJobs]int
	limits   int // How many command limits have passed since the start.
	pending  []lifecycle.Request
	faults   int
	restart  int

	violation string
}

func (m model) clone() model {
	m.pending = slices.Clone(m.pending)
	return m
}

func (m model) key() string {
	keys := make([]string, 0, len(m.pending))
	for _, r := range m.pending {
		keys = append(keys, fmt.Sprintf("%#v", r))
	}
	slices.Sort(keys)
	return fmt.Sprintf("%#v|%v|%v|%v|%v|%v|%v|%v|%v|%v|%v|%v|%v|%d|%d|%s", m.machine, m.jobs, m.runs, m.following, m.taken, m.died, m.away, m.forgetting, m.left, m.givenUp, m.attempts, m.ran, m.limits, m.faults, m.restart, strings.Join(keys, ";"))
}

func (m model) at() time.Time {
	return modelEpoch.Add(time.Duration(m.limits) * (modelLimit + time.Second))
}

// passLimit moves time past a limit. A command ends by its limit, whatever else is true of it.
func (m *model) passLimit() {
	m.limits++
	for i := range modelJobs {
		if m.runs[i].alive() && m.runs[i].phase == lifecycle.RunRunning {
			m.runs[i].phase = lifecycle.RunFinished
		}
	}
}

// search is one world's shape.
type search struct {
	stores   int
	depth    int
	faults   int
	restarts int
	// prior is how many cut-off attempts each job has when it is submitted, as a job the store had
	// queued again before a restart has, so that a search of a few moves reaches the bound.
	prior int
	refs  [modelJobs]lifecycle.Ref
	ids   [modelJobs]lifecycle.StoreID
	// step is the machine under check: lifecycle.Step, or a broken one the check must catch.
	step func(lifecycle.State, lifecycle.Event) (lifecycle.State, []lifecycle.Request)
}

func newSearch(stores, depth int) search {
	s := search{stores: stores, depth: depth, faults: 2, restarts: 1, step: lifecycle.Step}
	for i := range modelJobs {
		s.ids[i] = lifecycle.StoreIDOf("/repo", fmt.Sprintf("task%d", i))
	}
	for i := range modelJobs {
		s.refs[i] = lifecycle.Ref{Store: s.ids[s.store(i)], Job: core.JobID(fmt.Sprintf("j%d", i+1))}
	}
	return s
}

func (s search) store(i int) int { return i % s.stores }

func (s search) ref(i int) lifecycle.Ref { return s.refs[i] }

func (s search) index(ref lifecycle.Ref) int {
	return slices.Index(s.refs[:], ref)
}

func (s search) storeID(store int) lifecycle.StoreID { return s.ids[store] }

// policy is a job's rerun policy: job 0 is a check, which runs again after a proven death, and job
// 1 a command, which never runs twice.
func (s search) policy(i int) core.RerunPolicy {
	if i == 0 {
		return core.RerunAfterProvenDeath
	}
	return core.RerunBeforeStartOnly
}

// admitted is the job as the shell admits it, with what its store records.
func (s search) admitted(m *model, i int) lifecycle.Admitted {
	return lifecycle.Admitted{Ref: s.ref(i), SubmittedAt: submitted(i), Attempts: m.attempts[i], Policy: s.policy(i)}
}

// start is what a run's record proves of its start.
func start(r run) lifecycle.Start {
	if r.started {
		return lifecycle.MayHaveStarted{}
	}
	return lifecycle.NeverStarted{}
}

func submitted(i int) time.Time { return modelEpoch.Add(time.Duration(i+1) * time.Second) }

// presence is what a look finds of a run.
func (s search) presence(m *model, i int) lifecycle.Presence {
	r := m.runs[i]
	switch {
	case !r.visible():
		return lifecycle.RunNone{}
	case r.dead:
		return lifecycle.RunDead{Proof: lifecycle.ProofLockFree, Start: start(r)}
	}
	return lifecycle.RunAlive{Phase: r.phase, Limit: modelLimit}
}

// finding is what a look finds in a store, with what it found of the run. A job given up whose run
// is found here is taken back, as the shell does.
func (s search) finding(m *model, i int, p lifecycle.Presence) lifecycle.Finding {
	switch {
	case m.away[s.store(i)] || m.jobs[i] == gone:
		// A store given up is not there: the shell cannot read it.
		return lifecycle.StoreAway{}
	case m.jobs[i] == "":
		return lifecycle.NoSuchJob{}
	}
	if _, here := p.(lifecycle.RunAlive); here {
		m.givenUp[i] = false
	}
	return lifecycle.JobIn{
		State: m.jobs[i], SubmittedAt: submitted(i), GivenUp: m.givenUp[i] && m.jobs[i] == core.JobRunning,
		Attempts: m.attempts[i], Policy: s.policy(i),
	}
}

func (s search) look(m *model, i int) lifecycle.Recovered {
	return s.lookAs(m, i, false)
}

// lookAs is a look, and with unusable one that cannot use a supervisor that answers.
func (s search) lookAs(m *model, i int, unusable bool) lifecycle.Recovered {
	p := s.presence(m, i)
	if _, alive := p.(lifecycle.RunAlive); alive && unusable {
		p = lifecycle.RunUnknown{Limit: modelLimit}
	}
	return lifecycle.Recovered{Ref: s.ref(i), Finding: s.finding(m, i, p), Supervisor: p, At: m.at()}
}

func (m *model) fail(format string, args ...any) {
	if m.violation == "" {
		m.violation = fmt.Sprintf(format, args...)
	}
}

// feed steps the machine and checks what must hold after any event.
func (s search) feed(m *model, e lifecycle.Event) {
	var reqs []lifecycle.Request
	m.machine, reqs = s.step(m.machine, e)
	for _, r := range reqs {
		s.checkRequest(m, e, r)
		m.pending = append(m.pending, r)
	}
	s.checkWorld(m)
	// A look in flight may be what ends a hold that ran out, so this asks only once none is.
	if stranded := m.machine.Stranded(m.at()); len(stranded) != 0 && len(m.pending) == 0 {
		m.fail("nothing will start %v: %+v", stranded, m.machine.Held())
	}
}

// checkWorld checks what must hold of the world after any move, whether or not the machine heard
// of it: a supervisor can start a command on a request the machine made earlier.
func (s search) checkWorld(m *model) {
	commands := 0
	for _, r := range m.runs {
		if r.alive() && !r.apart && r.phase == lifecycle.RunRunning {
			commands++
		}
	}
	if commands > 1 {
		m.fail("two commands run at once: %+v", m.runs)
	}
	for i := range modelJobs {
		switch {
		case m.attempts[i] >= core.MaxAttempts:
			m.fail("job %d has %d attempts cut off, and a job gets %d", i, m.attempts[i], core.MaxAttempts)
		case s.policy(i) == core.RerunBeforeStartOnly && m.ran[i] > 1:
			m.fail("the command of job %d ran %d times, and a command job runs once", i, m.ran[i])
		case m.ran[i] > core.MaxAttempts-s.prior:
			m.fail("job %d ran %d times, past its attempts", i, m.ran[i])
		}
	}
	running := map[int]int{}
	for i, state := range m.jobs {
		if state == core.JobRunning {
			running[s.store(i)]++
		}
	}
	for store, n := range running {
		if n > 1 {
			m.fail("store %d shows %d jobs running", store, n)
		}
	}
}

func (s search) checkRequest(m *model, e lifecycle.Event, r lifecycle.Request) {
	i := s.index(r.Target())
	switch r := r.(type) {
	case lifecycle.Stop:
		if f, ok := e.(lifecycle.FollowFailed); ok && f.Cause == lifecycle.CauseStoreAway {
			m.fail("a store going away stopped job %s", r.Ref)
		}
	case lifecycle.Adopt:
		if m.jobs[i] != core.JobRunning {
			m.fail("adopted job %s, which its store has %q", r.Ref, m.jobs[i])
		}
	case lifecycle.Discard:
		if m.jobs[i] == core.JobRunning {
			m.fail("discarded the run of job %s, which its store has running", r.Ref)
		}
	case lifecycle.SignalCancel:
		if !m.away[s.store(i)] && m.jobs[i] != core.JobRunning {
			m.fail("signalled job %s, which its store has %q", r.Ref, m.jobs[i])
		}
	case lifecycle.LaunchRequest, lifecycle.Move, lifecycle.ArmRetry, lifecycle.Reexamine:
	}
}

// rank says when a healing world takes a move: the lowest first.
type rank int

const (
	answer rank = iota
	retry       // Answers that only come round again.
	ending      // A command ending, which waits for everything else.
	free        // What a caller or a fault does, which a healing world does not.
)

type move struct {
	label string
	rank  rank
	fault bool
	do    func(*model)
}

// moves lists what can happen next.
func (s search) moves(m *model) []move {
	var out []move
	add := func(r rank, fault bool, label string, do func(*model)) {
		out = append(out, move{label, r, fault, do})
	}
	for i := range modelJobs {
		ref := s.ref(i)
		store := s.store(i)
		switch {
		case m.jobs[i] == "":
			if !m.away[store] && (i == 0 || m.jobs[i-1] != "") {
				add(free, false, "submit"+digit(i), func(m *model) {
					m.jobs[i], m.attempts[i] = core.JobQueued, s.prior
					s.feed(m, s.admitted(m, i))
				})
			}
		case m.jobs[i] != gone && !m.jobs[i].Final() && !m.away[store] && !m.taken[i]:
			add(free, false, "cancel"+digit(i), func(m *model) {
				m.taken[i] = m.taken[i] || (m.jobs[i] == core.JobQueued && !s.decided(m, i))
				s.feed(m, lifecycle.CancelAsked{Ref: ref, Reason: "r", State: m.jobs[i], SubmittedAt: submitted(i), At: m.at()})
			})
		}
		r := m.runs[i]
		if r.alive() && r.phase == lifecycle.RunRunning {
			add(ending, false, "command"+digit(i)+" ends", func(m *model) { m.runs[i].phase = lifecycle.RunFinished })
		}
		if r.alive() && r.phase == lifecycle.RunFinished && m.following[i] {
			add(answer, false, "follow"+digit(i)+" reports", func(m *model) { s.finish(m, i) })
		}
		if r.alive() && !r.apart {
			add(free, true, "the run of job"+digit(i)+" goes out of sight", func(m *model) { m.runs[i].apart = true })
		}
		if r.alive() {
			add(free, true, "supervisor"+digit(i)+" dies", func(m *model) {
				m.runs[i].dead, m.died[i] = true, true
				if m.following[i] {
					m.following[i] = false
					s.feed(m, lifecycle.SupervisorLost{Ref: ref, Proof: lifecycle.ProofLockFree, Start: start(m.runs[i]), At: m.at()})
				}
			})
		}
		if m.following[i] && m.away[store] {
			add(answer, false, "store away ends follow"+digit(i), func(m *model) {
				m.following[i] = false
				s.feed(m, lifecycle.FollowFailed{Ref: ref, Cause: lifecycle.CauseStoreAway, Failure: modelStop, At: m.at()})
			})
		}
		if m.following[i] {
			add(free, true, "supervisor"+digit(i)+" is said to be gone", func(m *model) {
				m.following[i], m.died[i] = false, true
				s.feed(m, lifecycle.SupervisorLost{Ref: ref, At: m.at()})
			})
			add(free, true, "supervisor"+digit(i)+" stops answering", func(m *model) {
				m.following[i], m.died[i] = false, true
				s.feed(m, lifecycle.FollowFailed{Ref: ref, Cause: lifecycle.CauseSupervisorSilent, Failure: modelStop, At: m.at()})
			})
		}
	}
	for store := range s.stores {
		id := s.storeID(store)
		unread := slices.Contains(m.machine.Unread(), id)
		switch {
		case m.away[store] && !unread:
			add(answer, false, "sweep marks store"+digit(store)+" unread", func(m *model) {
				s.feed(m, lifecycle.StoreUnread{Store: id, Failure: modelStop})
			})
		case !m.away[store] && unread:
			add(answer, false, "sweep reads store"+digit(store)+" again", func(m *model) { s.readAgain(m, store) })
		case !m.away[store]:
			for i := range modelJobs {
				if s.store(i) != store || s.held(m, i) {
					continue
				}
				switch m.jobs[i] {
				case core.JobQueued:
					add(answer, false, "sweep admits"+digit(i), func(m *model) {
						s.feed(m, s.admitted(m, i))
					})
				case core.JobRunning:
					add(answer, false, "sweep looks at"+digit(i), func(m *model) { s.feed(m, s.look(m, i)) })
				}
			}
		}
		if !m.away[store] {
			add(free, true, "store"+digit(store)+" goes away", func(m *model) { m.away[store] = true })
		} else {
			add(answer, false, "store"+digit(store)+" returns", func(m *model) { m.away[store] = false })
		}
		if s.mayGiveUp(m, store) {
			// The daemon names this way out, so a healing world takes it.
			add(answer, false, "the owner record of store"+digit(store)+" is removed", func(m *model) { s.giveUp(m, store) })
		}
		switch {
		case m.forgetting[store]:
			add(answer, false, "the machine forgets store"+digit(store), func(m *model) { s.forgotten(m, store) })
		case m.away[store]:
			add(free, true, "store"+digit(store)+" is forgotten", func(m *model) { s.endRuns(m, store, false) })
			add(free, true, "store"+digit(store)+" is forgotten, a command left running", func(m *model) { s.endRuns(m, store, true) })
		}
	}
	if m.limits == 0 {
		add(free, false, "the limit passes", (*model).passLimit)
	}
	seen := map[string]bool{}
	for at, r := range m.pending {
		label := fmt.Sprintf("%T", r) + r.Target().String()
		if seen[label] {
			continue
		}
		seen[label] = true
		s.answers(m, at, r, add)
	}
	if m.restart < s.restarts {
		add(free, false, "restart", func(m *model) { s.restartDaemon(m, false) })
		add(free, true, "restart with no readable supervisors", func(m *model) { s.restartDaemon(m, true) })
	}
	return out
}

// mayGiveUp says a user would remove the owner record of a store: the daemon tells them to, of every
// job of the store that runs, and every run of the store out of sight is dead, as the place that
// ran it is gone for good.
func (s search) mayGiveUp(m *model, store int) bool {
	if m.away[store] {
		return false
	}
	running := false
	for i := range modelJobs {
		if s.store(i) != store {
			continue
		}
		if m.runs[i].apart && m.runs[i].alive() {
			return false
		}
		if m.jobs[i] != core.JobRunning {
			continue
		}
		if !slices.ContainsFunc(m.machine.Troubled(), func(t lifecycle.Trouble) bool {
			return t.Ref == s.ref(i) && strings.Contains(t.Failure.Message, "owner record")
		}) {
			return false
		}
		running = running || !m.givenUp[i]
	}
	return running
}

// giveUp is the store taken over after its owner record was removed: every job it has running is
// given up, and stays so while it runs.
func (s search) giveUp(m *model, store int) {
	for i := range modelJobs {
		if s.store(i) == store && m.jobs[i] == core.JobRunning {
			m.givenUp[i] = true
		}
	}
}

// placesGone is the world in which every place a run is out of sight in is gone for good: a running
// job whose run is out of sight is held in doubt until the user gives that place up, which is the
// way out the daemon names, and a user does that once the place is gone.
func (s search) placesGone(m *model) {
	for i := range modelJobs {
		if !m.runs[i].apart || !m.runs[i].alive() {
			continue
		}
		m.runs[i].dead, m.died[i] = true, true
		if m.following[i] {
			m.following[i] = false
			s.feed(m, lifecycle.SupervisorLost{Ref: s.ref(i), Proof: lifecycle.ProofLockFree, Start: start(m.runs[i]), At: m.at()})
		}
	}
}

// held says whether the machine holds a job.
func (s search) held(m *model, i int) bool {
	return slices.ContainsFunc(m.machine.Held(), func(h lifecycle.Held) bool { return h.Ref == s.ref(i) })
}

// decided says the machine settled on a job's end, which a late cancel does not change.
func (s search) decided(m *model, i int) bool {
	return slices.ContainsFunc(m.machine.Held(), func(h lifecycle.Held) bool {
		return h.Ref == s.ref(i) && (h.Phase == lifecycle.PhaseSettling || h.Phase == lifecycle.PhaseCancelling || h.Phase == lifecycle.PhaseUnrecorded)
	})
}

// finish is a follow telling the machine how a finished command ended.
func (s search) finish(m *model, i int) {
	m.following[i] = false
	kind := core.ExecExited
	if m.runs[i].cancel {
		kind = core.ExecCancelled
	}
	s.feed(m, lifecycle.Finished{Ref: s.ref(i), At: m.at(), Outcome: lifecycle.Outcome{
		End: lifecycle.CommandEnded{Attempt: core.Ended{Exec: core.ExecOutcome{Kind: kind}}}, CancelAsked: m.runs[i].cancel, CancelReason: "r",
	}})
}

func (s search) readAgain(m *model, store int) {
	for i := range modelJobs {
		if s.store(i) == store && (m.jobs[i] == core.JobQueued || m.jobs[i] == core.JobRunning) {
			s.feed(m, s.look(m, i))
		}
	}
	s.feed(m, lifecycle.StoreRead{Store: s.storeID(store)})
}

// endRuns is the first half of giving up a store, as the shell does it: each run of the store is
// cancelled and waited for, and a command that does not end in time is left running. A parked
// supervisor never ends on its own, so it is left too. The machine is told later, by forgotten.
func (s search) endRuns(m *model, store int, leaveOne bool) {
	m.forgetting[store] = true
	for i := range modelJobs {
		r := &m.runs[i]
		if s.store(i) != store || !r.alive() || r.apart {
			continue
		}
		r.cancel = true
		switch {
		case r.phase == lifecycle.RunParked:
			m.left[i] = r.phase
		case r.phase == lifecycle.RunRunning && leaveOne:
			m.left[i], leaveOne = r.phase, false
		case r.phase == lifecycle.RunRunning:
			r.phase = lifecycle.RunFinished
		}
	}
}

// forgotten is the second half: the machine forgets the store's jobs and hears of the runs left,
// the other runs are released, and the store's jobs are gone for good.
func (s search) forgotten(m *model, store int) {
	m.pending = slices.DeleteFunc(m.pending, func(r lifecycle.Request) bool { return s.store(s.index(r.Target())) == store })
	e := lifecycle.StoreForgotten{Store: s.storeID(store), At: m.at()}
	for i := range modelJobs {
		if s.store(i) != store {
			continue
		}
		switch {
		case m.left[i] != "":
			e.Left = append(e.Left, lifecycle.LeftRun{Ref: s.ref(i), Supervisor: lifecycle.RunAlive{Phase: m.left[i], Limit: modelLimit}})
		case !m.runs[i].apart:
			m.runs[i] = run{}
		}
		m.jobs[i], m.following[i], m.left[i] = gone, false, ""
	}
	m.away[store], m.forgetting[store] = false, false
	s.feed(m, e)
}

// restartDaemon is Start: every store read, or marked unread, then what is left of the runs.
func (s search) restartDaemon(m *model, unreadable bool) {
	m.restart++
	m.machine, m.pending = lifecycle.Recovering(), nil
	m.following, m.taken = [modelJobs]bool{}, [modelJobs]bool{}
	// A forget the daemon was in the middle of ends with it; what it did to the runs stays.
	m.forgetting, m.left = [modelJobs]bool{}, [modelJobs]lifecycle.RunPhase{}
	look := func(i int) lifecycle.Recovered { return s.lookAs(m, i, unreadable) }
	for store := range s.stores {
		if m.away[store] || s.hasLeftRun(m, store) {
			// A store given up is not in the index, and a run of it is looked for in it, which
			// cannot be read.
			s.feed(m, lifecycle.StoreUnread{Store: s.storeID(store), Failure: modelStop})
		}
	}
	for i := range modelJobs {
		if m.jobs[i] == gone && m.runs[i].visible() {
			s.feed(m, look(i))
		}
		if m.jobs[i] == "" || m.jobs[i] == gone {
			continue
		}
		if !m.away[s.store(i)] && (m.jobs[i] == core.JobQueued || m.jobs[i] == core.JobRunning) || m.runs[i].visible() {
			s.feed(m, look(i))
		}
	}
	s.feed(m, lifecycle.RecoveryDone{})
}

// hasLeftRun says a store was given up with a run of it left.
func (s search) hasLeftRun(m *model, store int) bool {
	for i := range modelJobs {
		if s.store(i) == store && m.jobs[i] == gone && m.runs[i].visible() {
			return true
		}
	}
	return false
}

// answers lists how the shell can answer one request.
func (s search) answers(m *model, at int, r lifecycle.Request, add func(rank, bool, string, func(*model))) {
	i := s.index(r.Target())
	ref := r.Target()
	away := m.away[s.store(i)]
	name := fmt.Sprintf("%T", r) + digit(i)
	take := func(m *model) { m.pending = slices.Delete(m.pending, at, at+1) }
	switch r := r.(type) {
	case lifecycle.LaunchRequest:
		switch {
		case away:
			add(answer, false, name+" store away", func(m *model) {
				take(m)
				s.feed(m, lifecycle.LaunchFailed{Ref: ref, Cause: lifecycle.CauseStoreAway, Failure: modelStop, At: m.at()})
			})
		case m.jobs[i] != core.JobQueued:
			add(answer, false, name+" not queued", func(m *model) { take(m); s.feed(m, lifecycle.Closed{Ref: ref}) })
		default:
			add(answer, false, name+" ok", func(m *model) {
				take(m)
				m.runs[i], m.jobs[i] = run{phase: lifecycle.RunParked}, core.JobRunning
				s.feed(m, lifecycle.Launched{Ref: ref})
			})
			add(answer, true, name+" refused", func(m *model) {
				take(m)
				s.feed(m, lifecycle.LaunchFailed{Ref: ref, Cause: lifecycle.CauseRefused, Failure: modelStop, At: m.at()})
			})
			add(answer, true, name+" supervisor never came up", func(m *model) {
				take(m)
				m.runs[i] = run{phase: lifecycle.RunParked, dead: true}
				s.feed(m, lifecycle.LaunchFailed{Ref: ref, Cause: lifecycle.CauseSupervisorSilent, Failure: modelStop, At: m.at()})
			})
			add(answer, true, name+" parked, store lost", func(m *model) {
				take(m)
				m.runs[i] = run{phase: lifecycle.RunParked}
				s.feed(m, lifecycle.LaunchFailed{Ref: ref, Cause: lifecycle.CauseStoreAway, Failure: modelStop, At: m.at()})
			})
			add(answer, true, name+" recorded, reply lost", func(m *model) {
				take(m)
				m.runs[i], m.jobs[i] = run{phase: lifecycle.RunParked}, core.JobRunning
				s.feed(m, lifecycle.LaunchFailed{Ref: ref, Cause: lifecycle.CauseStoreAway, Failure: modelStop, At: m.at()})
			})
		}
	case lifecycle.Adopt:
		add(answer, false, name, func(m *model) {
			take(m)
			if !m.runs[i].alive() {
				if m.runs[i].phase != "" {
					s.feed(m, lifecycle.SupervisorLost{Ref: ref, Proof: lifecycle.ProofLockFree, Start: start(m.runs[i]), At: m.at()})
				}
				return
			}
			m.following[i] = true
			m.runs[i].cancel = m.runs[i].cancel || r.CancelFirst
			if m.runs[i].phase != lifecycle.RunParked {
				return
			}
			m.runs[i].started = true
			if m.runs[i].cancel {
				// A supervisor told to cancel while parked ends without running the command.
				m.runs[i].phase = lifecycle.RunFinished
				return
			}
			if m.taken[i] {
				m.fail("job %s started after its cancel was taken while it was queued", ref)
			}
			m.runs[i].phase, m.runs[i].started = lifecycle.RunRunning, true
			m.ran[i]++
		})
	case lifecycle.SignalCancel:
		add(answer, false, name, func(m *model) {
			take(m)
			if m.runs[i].alive() {
				m.runs[i].cancel = true
				return
			}
			s.feed(m, lifecycle.CancelFailed{Ref: ref})
		})
		add(answer, true, name+" did not arrive", func(m *model) { take(m); s.feed(m, lifecycle.CancelFailed{Ref: ref}) })
	case lifecycle.Stop:
		add(answer, false, name, func(m *model) {
			take(m)
			if !m.runs[i].alive() {
				s.feed(m, lifecycle.SupervisorLost{Ref: ref, Proof: lifecycle.ProofLockFree, Start: start(m.runs[i]), At: m.at()})
				return
			}
			m.runs[i].cancel = true
			if m.runs[i].phase != lifecycle.RunFinished {
				m.runs[i].phase = lifecycle.RunFinished
			}
			s.finish(m, i)
		})
		add(answer, true, name+" failed", func(m *model) {
			take(m)
			s.feed(m, lifecycle.StopFailed{Ref: ref, Failure: modelStop})
		})
	case lifecycle.ArmRetry:
		add(retry, false, name, func(m *model) { take(m); s.feed(m, lifecycle.RetryDue{Ref: ref}) })
	case lifecycle.Reexamine:
		// A look at a run left of a store given up finds nothing new until its command ends, so a
		// healing world takes it with the commands that end.
		look := retry
		if m.jobs[i] == gone {
			look = ending
		}
		add(look, false, name, func(m *model) { take(m); s.feed(m, s.look(m, i)) })
		add(retry, true, name+" finds nothing usable", func(m *model) { take(m); s.feed(m, s.lookAs(m, i, true)) })
	case lifecycle.Move:
		if away {
			add(answer, false, name+" store away", func(m *model) {
				take(m)
				s.feed(m, lifecycle.MoveFailed{Ref: ref, Failure: modelStop})
			})
			return
		}
		add(answer, false, name, func(m *model) { take(m); s.settle(m, i, r) })
		add(answer, true, name+" refused", func(m *model) {
			take(m)
			s.feed(m, lifecycle.MoveFailed{Ref: ref, Failure: modelStop})
		})
	case lifecycle.Discard:
		add(answer, false, name, func(m *model) {
			take(m)
			if m.runs[i].alive() && m.runs[i].phase == lifecycle.RunRunning {
				s.feed(m, lifecycle.DiscardFailed{Ref: ref, Failure: modelStop}) // A running command is not released.
				return
			}
			m.runs[i] = run{}
			if r.Queued {
				s.feed(m, lifecycle.Discarded{Ref: ref, At: m.at()})
				return
			}
			s.feed(m, lifecycle.Closed{Ref: ref})
		})
		add(answer, true, name+" failed", func(m *model) {
			take(m)
			s.feed(m, lifecycle.DiscardFailed{Ref: ref, Failure: modelStop})
		})
	}
}

// settle is the shell's move: the first end a store records is the end. A settlement that queues
// the job again is recorded only on a job its store has running, and answered with Requeued.
func (s search) settle(m *model, i int, r lifecycle.Move) {
	t := r.Settlement.Transition
	if r.Settlement.Requeues() {
		s.requeue(m, i, r)
		return
	}
	if !m.jobs[i].Final() {
		// A job given up may be lost with a supervisor that cannot be used, as long as its command
		// does not run: the user gave up its outcome, and no command is lost.
		mayLose := m.givenUp[i] && m.runs[i].phase != lifecycle.RunRunning
		if t.To == core.JobLost && m.runs[i].alive() && !mayLose {
			m.fail("job %d is lost with a live supervisor", i)
		}
		t.From = m.jobs[i]
		job := core.Job{ID: t.Job, State: m.jobs[i], Spec: core.JobSpec{Settings: core.CommandSettings{}}}
		if _, err := job.Apply(t); err != nil {
			m.fail("the store refuses the end of job %d from %q: %v", i, m.jobs[i], err)
		}
		m.jobs[i] = t.To
	}
	if r.Release {
		switch {
		case m.runs[i].alive() && m.runs[i].phase == lifecycle.RunRunning:
			m.fail("job %d ended with its command running", i)
		case m.runs[i].phase != "":
			m.runs[i] = run{}
		}
	}
	s.feed(m, lifecycle.Closed{Ref: s.ref(i)})
}

// requeue records a cut-off attempt and checks that the machine asked for it only with the proof a
// rerun needs: the run is dead, and a job that runs only before its start never started.
func (s search) requeue(m *model, i int, r lifecycle.Move) {
	t := r.Settlement.Transition
	if m.jobs[i] != core.JobRunning {
		// The store has the job elsewhere: the move does not apply, and the daemon is done with it.
		s.feed(m, lifecycle.Closed{Ref: s.ref(i)})
		return
	}
	switch _, before := t.Cut.(core.CutBeforeStart); {
	case m.runs[i].alive():
		m.fail("job %d is queued again with a live supervisor", i)
	case before && m.runs[i].started:
		m.fail("job %d is queued again as never started, and its command was told to start", i)
	case !before && s.policy(i) != core.RerunAfterProvenDeath:
		m.fail("command job %d is queued again after its command may have started", i)
	}
	job := core.Job{ID: t.Job, State: m.jobs[i], Spec: core.JobSpec{Settings: core.CommandSettings{}}, Attempts: make([]core.PastAttempt, m.attempts[i])}
	if _, err := job.Apply(t); err != nil {
		m.fail("the store refuses to queue job %d again: %v", i, err)
		return
	}
	m.jobs[i] = core.JobQueued
	m.attempts[i]++
	s.feed(m, lifecycle.Requeued{Ref: s.ref(i)})
}

// heal lets a world run with no more faults or callers, and returns false if it does not end. A
// store left away stays away, and each of its retries comes round a few times only. A world that
// only retries for a while waits: time passes, by a limit at a time.
func (s search) heal(m *model, awayFor int) bool {
	rounds, idle, waits := 0, 0, 0
	for steps := 0; steps < 200; steps++ {
		if m.violation != "" {
			return true
		}
		var pick *move
		for _, c := range s.moves(m) {
			switch {
			case c.fault, c.rank == free:
				continue
			case awayFor >= 0 && c.label == "store"+digit(awayFor)+" returns":
				continue
			case c.rank == retry && (awayFor >= 0 && rounds >= 8 || idle >= 8):
				continue
			}
			if pick == nil || c.rank < pick.rank {
				pick = &c
			}
		}
		if pick == nil && idle >= 8 && awayFor < 0 && waits < 2 {
			m.passLimit()
			idle, waits = 0, waits+1
			continue
		}
		if pick == nil {
			return true
		}
		if pick.rank == retry {
			rounds++
			idle++
		} else {
			idle = 0
		}
		if pick.rank == ending {
			s.checkCancels(m)
		}
		pick.do(m)
		s.checkWorld(m)
	}
	return false
}

// checkCancels is asked when only commands ending are left: a cancel the machine holds has reached
// the supervisor of the job by then.
func (s search) checkCancels(m *model) {
	for _, h := range m.machine.Held() {
		i := s.index(h.Ref)
		if h.CancelAsked && m.runs[i].alive() && m.runs[i].phase == lifecycle.RunRunning && !m.runs[i].cancel {
			m.fail("job %s runs on with its cancel undelivered", h.Ref)
		}
	}
}

// healed says what must be true of a world healed of everything.
func (s search) healed(m model) string {
	for i := range modelJobs {
		if m.away[s.store(i)] {
			m.away[s.store(i)] = false
		}
	}
	// A run left of a store given up, which a restart found, holds that store's job in doubt until
	// the user gives the store up again, which is the way out the daemon names.
	for store := range s.stores {
		if s.hasLeftRun(&m, store) && slices.ContainsFunc(m.machine.Held(), func(h lifecycle.Held) bool {
			return h.Ref.Store == s.storeID(store) && h.Phase == lifecycle.PhaseInDoubt
		}) {
			s.endRuns(&m, store, false)
			s.forgotten(&m, store)
		}
	}
	s.placesGone(&m)
	if !s.heal(&m, -1) {
		return fmt.Sprintf("it never settles: %+v, jobs %v", m.machine.Held(), m.jobs)
	}
	if m.violation != "" {
		return m.violation
	}
	if held := m.machine.Held(); len(held) != 0 {
		return fmt.Sprintf("jobs are held with nothing owed: %+v, jobs %v", held, m.jobs)
	}
	for i, state := range m.jobs {
		if state != "" && state != gone && !state.Final() {
			return fmt.Sprintf("job %d stays %q", i, state)
		}
		if m.taken[i] && !m.died[i] && state != gone && state != core.JobCancelled {
			return fmt.Sprintf("job %d ended %q although its cancel was taken while it was queued", i, state)
		}
	}
	return ""
}

// strandedBy says what must be true of a world in which a store never returns: the other store's
// jobs end, and each of its own has a stated wait or an unread store.
func (s search) strandedBy(m model, store int) string {
	m.away[store] = true
	s.placesGone(&m)
	for range 2 {
		if !s.heal(&m, store) {
			return fmt.Sprintf("the rest never settles with store %d gone: %+v, jobs %v", store, m.machine.Held(), m.jobs)
		}
		m.passLimit()
	}
	if !s.heal(&m, store) {
		return fmt.Sprintf("the rest never settles with store %d gone: %+v, jobs %v", store, m.machine.Held(), m.jobs)
	}
	if m.violation != "" {
		return m.violation
	}
	told := map[lifecycle.Ref]bool{}
	for _, t := range append(m.machine.Waiting(), m.machine.Troubled()...) {
		told[t.Ref] = true
	}
	for i, state := range m.jobs {
		switch {
		case state == "" || state == gone || state.Final():
		case s.store(i) != store:
			return fmt.Sprintf("job %d of another store stays %q with store %d gone", i, state, store)
		case !slices.Contains(m.machine.Unread(), s.storeID(store)) && !told[s.ref(i)] && s.held(&m, i):
			return "job" + digit(i) + " waits with no reason given"
		}
	}
	return ""
}

type step struct {
	parent string
	label  string
}

func digit(i int) string { return " " + string(rune('0'+i)) }

// walked is what a walk found: the first violation, with the moves that led to it, how many
// states it saw, and how many of them have a job queued again.
type walked struct {
	violation string
	states    int
	requeued  int
}

func (s search) walk(t *testing.T) walked {
	t.Helper()
	w := s.explore()
	if w.violation != "" {
		t.Fatal(w.violation)
	}
	t.Logf("%d states to depth %d, %d with a job queued again", w.states, s.depth, w.requeued)
	return w
}

// explore walks every order of moves to the search's depth, and stops at the first violation.
func (s search) explore() walked {
	start := model{machine: lifecycle.Recovering()}
	s.feed(&start, lifecycle.RecoveryDone{})
	seen := map[string]step{start.key(): {}}
	trace := func(key, last string) string {
		path := []string{last}
		for key != "" && seen[key].label != "" {
			path = append(path, seen[key].label)
			key = seen[key].parent
		}
		slices.Reverse(path)
		return strings.Join(path, " -> ")
	}
	var out walked
	fail := func(why, key, last string) walked {
		out.violation, out.states = fmt.Sprintf("%s\nafter: %s", why, trace(key, last)), len(seen)
		return out
	}
	frontier := []model{start}
	for depth := 0; depth < s.depth && len(frontier) > 0; depth++ {
		var next []model
		for _, m := range frontier {
			key := m.key()
			if why := s.healed(m.clone()); why != "" {
				return fail(why, key, "(healing)")
			}
			for store := range s.stores {
				// Only a store already away: from any other, one more move makes it so.
				if s.stores > 1 && m.away[store] {
					if why := s.strandedBy(m.clone(), store); why != "" {
						return fail(why, key, "(store gone)")
					}
				}
			}
			for _, c := range s.moves(&m) {
				if c.fault && m.faults >= s.faults {
					continue
				}
				n := m.clone()
				c.do(&n)
				s.checkWorld(&n)
				if n.violation != "" {
					return fail(n.violation, key, c.label)
				}
				if c.fault {
					n.faults++
				}
				k := n.key()
				if _, ok := seen[k]; ok {
					continue
				}
				seen[k] = step{key, c.label}
				if len(seen) > modelStates {
					return fail(fmt.Sprintf("more than %d states", modelStates), key, c.label)
				}
				if slices.ContainsFunc(n.attempts[:], func(a int) bool { return a > s.prior }) {
					out.requeued++
				}
				next = append(next, n)
			}
		}
		frontier = next
	}
	out.states = len(seen)
	return out
}

func TestEveryOrderOfEventsForTwoJobsInOneStore(t *testing.T) {
	newSearch(1, 10).walk(t)
}

func TestEveryOrderOfEventsForTwoStores(t *testing.T) {
	newSearch(2, 9).walk(t)
}

// withPrior is a search whose jobs come with attempts already cut off, as a job its store queued
// again before a restart does, so that a few moves reach the bound.
func withPrior(prior, depth int) search {
	s := newSearch(1, depth)
	s.prior = prior
	return s
}

// A job with attempts cut off before is queued again only while it has attempts left: on its last
// attempt, checkWorld fails any order of events that queues it again.
func TestEveryOrderOfEventsForJobsWithAttemptsCutOff(t *testing.T) {
	for prior := 1; prior < core.MaxAttempts; prior++ {
		last := prior == core.MaxAttempts-1
		depth := 9
		if last {
			depth = 8
		}
		if w := withPrior(prior, depth).walk(t); !last && w.requeued == 0 {
			t.Errorf("with %d attempts cut off before, no order of events queued a job again", prior)
		}
	}
}

// The model check catches a machine that breaks the rule: each mutant changes what the machine is
// told the way a bug in it or in the shell would, and the check must find a violation.
func TestTheModelCheckCatchesABrokenRetryRule(t *testing.T) {
	type mutant struct {
		prior  int
		mutate func(lifecycle.Event) lifecycle.Event
	}
	mutants := map[string]mutant{
		// The record's start mark is ignored: every run is taken for one that never started.
		"never started is assumed": {0, func(e lifecycle.Event) lifecycle.Event {
			switch e := e.(type) {
			case lifecycle.SupervisorLost:
				e.Start = lifecycle.NeverStarted{}
				return e
			case lifecycle.Recovered:
				if d, ok := e.Supervisor.(lifecycle.RunDead); ok {
					d.Start = lifecycle.NeverStarted{}
					e.Supervisor = d
				}
				return e
			}
			return e
		}},
		// The attempts a store records are not counted, so the bound never comes.
		"attempts are not counted": {core.MaxAttempts - 1, func(e lifecycle.Event) lifecycle.Event {
			switch e := e.(type) {
			case lifecycle.Admitted:
				e.Attempts = 0
				return e
			case lifecycle.Recovered:
				if f, ok := e.Finding.(lifecycle.JobIn); ok {
					f.Attempts = 0
					e.Finding = f
				}
				return e
			}
			return e
		}},
	}
	for name, m := range mutants {
		t.Run(name, func(t *testing.T) {
			s := withPrior(m.prior, 7)
			s.step = func(st lifecycle.State, e lifecycle.Event) (lifecycle.State, []lifecycle.Request) {
				return lifecycle.Step(st, m.mutate(e))
			}
			w := s.explore()
			if w.violation == "" {
				t.Fatalf("the model check found nothing wrong in %d states", w.states)
			}
			t.Logf("caught: %s", w.violation)
		})
	}
}
