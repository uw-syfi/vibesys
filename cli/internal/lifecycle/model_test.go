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
}

func (r run) alive() bool { return r.phase != "" && !r.dead }

// model is the world at one moment.
type model struct {
	machine   lifecycle.State
	jobs      [modelJobs]core.JobState
	runs      [modelJobs]run
	following [modelJobs]bool
	// taken says the machine took a cancel while the job's store had it queued.
	taken [modelJobs]bool
	// died says its supervisor died or went silent, which can decide a job's end before a cancel.
	died    [modelJobs]bool
	away    [modelJobs]bool // By store.
	limits  int             // How many command limits have passed since the start.
	pending []lifecycle.Request
	faults  int
	restart int

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
	return fmt.Sprintf("%#v|%v|%v|%v|%v|%v|%v|%v|%d|%d|%s", m.machine, m.jobs, m.runs, m.following, m.taken, m.died, m.away, m.limits, m.faults, m.restart, strings.Join(keys, ";"))
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
	refs     [modelJobs]lifecycle.Ref
	ids      [modelJobs]lifecycle.StoreID
}

func newSearch(stores, depth int) search {
	s := search{stores: stores, depth: depth, faults: 2, restarts: 1}
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

func submitted(i int) time.Time { return modelEpoch.Add(time.Duration(i+1) * time.Second) }

// presence is what a look finds of a run.
func (s search) presence(m *model, i int) lifecycle.Presence {
	r := m.runs[i]
	switch {
	case r.phase == "":
		return lifecycle.RunNone{}
	case r.dead:
		return lifecycle.RunDead{Proof: lifecycle.ProofLockFree}
	}
	return lifecycle.RunAlive{Phase: r.phase, Limit: modelLimit}
}

// finding is what a look finds in a store.
func (s search) finding(m *model, i int) lifecycle.Finding {
	switch {
	case m.away[s.store(i)]:
		return lifecycle.StoreAway{}
	case m.jobs[i] == "" || m.jobs[i] == gone:
		return lifecycle.NoSuchJob{}
	}
	return lifecycle.JobIn{State: m.jobs[i], SubmittedAt: submitted(i)}
}

func (s search) look(m *model, i int) lifecycle.Recovered {
	return lifecycle.Recovered{Ref: s.ref(i), Finding: s.finding(m, i), Supervisor: s.presence(m, i), At: m.at()}
}

func (m *model) fail(format string, args ...any) {
	if m.violation == "" {
		m.violation = fmt.Sprintf(format, args...)
	}
}

// feed steps the machine and checks what must hold after any event.
func (s search) feed(m *model, e lifecycle.Event) {
	var reqs []lifecycle.Request
	m.machine, reqs = lifecycle.Step(m.machine, e)
	for _, r := range reqs {
		s.checkRequest(m, e, r)
		m.pending = append(m.pending, r)
	}
	commands := 0
	for _, r := range m.runs {
		if r.alive() && r.phase == lifecycle.RunRunning {
			commands++
		}
	}
	if commands > 1 {
		m.fail("two commands run at once: %+v", m.runs)
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
	// A look in flight may be what ends a hold that ran out, so this asks only once none is.
	if stranded := m.machine.Stranded(m.at()); len(stranded) != 0 && len(m.pending) == 0 {
		m.fail("nothing will start %v: %+v", stranded, m.machine.Held())
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
					m.jobs[i] = core.JobQueued
					s.feed(m, lifecycle.Admitted{Ref: ref, SubmittedAt: submitted(i)})
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
		if r.alive() {
			add(free, true, "supervisor"+digit(i)+" dies", func(m *model) {
				m.runs[i].dead, m.died[i] = true, true
				if m.following[i] {
					m.following[i] = false
					s.feed(m, lifecycle.SupervisorLost{Ref: ref, Proof: lifecycle.ProofLockFree, At: m.at()})
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
						s.feed(m, lifecycle.Admitted{Ref: s.ref(i), SubmittedAt: submitted(i)})
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
			add(free, true, "store"+digit(store)+" is forgotten", func(m *model) { s.forget(m, store) })
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
		End: lifecycle.Ran{Exec: core.ExecOutcome{Kind: kind}}, CancelAsked: m.runs[i].cancel, CancelReason: "r",
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

// forget gives up a store: its jobs leave the machine and their runs end.
func (s search) forget(m *model, store int) {
	s.feed(m, lifecycle.StoreForgotten{Store: s.storeID(store)})
	m.away[store] = false
	for i := range modelJobs {
		if s.store(i) == store {
			m.jobs[i], m.runs[i], m.following[i] = gone, run{}, false
		}
	}
	m.pending = slices.DeleteFunc(m.pending, func(r lifecycle.Request) bool { return s.store(s.index(r.Target())) == store })
}

// restartDaemon is Start: every store read, or marked unread, then what is left of the runs.
func (s search) restartDaemon(m *model, unreadable bool) {
	m.restart++
	m.machine, m.pending = lifecycle.Recovering(), nil
	m.following, m.taken = [modelJobs]bool{}, [modelJobs]bool{}
	look := func(i int) lifecycle.Recovered {
		e := s.look(m, i)
		if _, none := e.Supervisor.(lifecycle.RunNone); unreadable && !none {
			e.Supervisor = lifecycle.RunUnknown{Limit: modelLimit}
		}
		return e
	}
	for store := range s.stores {
		if m.away[store] {
			s.feed(m, lifecycle.StoreUnread{Store: s.storeID(store), Failure: modelStop})
		}
	}
	for i := range modelJobs {
		if m.jobs[i] == "" || m.jobs[i] == gone {
			continue
		}
		if _, none := look(i).Supervisor.(lifecycle.RunNone); !m.away[s.store(i)] && (m.jobs[i] == core.JobQueued || m.jobs[i] == core.JobRunning) || !none {
			s.feed(m, look(i))
		}
	}
	s.feed(m, lifecycle.RecoveryDone{})
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
					s.feed(m, lifecycle.SupervisorLost{Ref: ref, Proof: lifecycle.ProofLockFree, At: m.at()})
				}
				return
			}
			m.following[i] = true
			m.runs[i].cancel = m.runs[i].cancel || r.CancelFirst
			if m.runs[i].phase != lifecycle.RunParked {
				return
			}
			if m.runs[i].cancel {
				// A supervisor told to cancel while parked ends without running the command.
				m.runs[i].phase = lifecycle.RunFinished
				return
			}
			if m.taken[i] {
				m.fail("job %s started after its cancel was taken while it was queued", ref)
			}
			m.runs[i].phase = lifecycle.RunRunning
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
				s.feed(m, lifecycle.SupervisorLost{Ref: ref, Proof: lifecycle.ProofLockFree, At: m.at()})
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
		add(retry, false, name, func(m *model) { take(m); s.feed(m, s.look(m, i)) })
		add(retry, true, name+" finds nothing usable", func(m *model) {
			take(m)
			e := s.look(m, i)
			if _, alive := e.Supervisor.(lifecycle.RunAlive); alive {
				e.Supervisor = lifecycle.RunUnknown{Limit: modelLimit}
			}
			s.feed(m, e)
		})
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

// settle is the shell's move: the first end a store records is the end.
func (s search) settle(m *model, i int, r lifecycle.Move) {
	t := r.Settlement.Transition
	if !m.jobs[i].Final() {
		if t.To == core.JobLost && m.runs[i].alive() {
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

// heal lets a world run with no more faults or callers, and returns false if it does not end. A
// store left away stays away, and each of its retries comes round a few times only.
func (s search) heal(m *model, awayFor int) bool {
	rounds := 0
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
			case c.rank == retry && awayFor >= 0 && rounds >= 8:
				continue
			}
			if pick == nil || c.rank < pick.rank {
				pick = &c
			}
		}
		if pick == nil {
			return true
		}
		if pick.rank == retry {
			rounds++
		}
		if pick.rank == ending {
			s.checkCancels(m)
		}
		pick.do(m)
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

func (s search) walk(t *testing.T) {
	t.Helper()
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
	frontier := []model{start}
	for depth := 0; depth < s.depth && len(frontier) > 0; depth++ {
		var next []model
		for _, m := range frontier {
			key := m.key()
			if why := s.healed(m.clone()); why != "" {
				t.Fatalf("%s\nafter: %s", why, trace(key, "(healing)"))
			}
			for store := range s.stores {
				// Only a store already away: from any other, one more move makes it so.
				if s.stores > 1 && m.away[store] {
					if why := s.strandedBy(m.clone(), store); why != "" {
						t.Fatalf("%s\nafter: %s", why, trace(key, "(store gone)"))
					}
				}
			}
			for _, c := range s.moves(&m) {
				if c.fault && m.faults >= s.faults {
					continue
				}
				n := m.clone()
				c.do(&n)
				if n.violation != "" {
					t.Fatalf("%s\nafter: %s", n.violation, trace(key, c.label))
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
					t.Fatalf("more than %d states", modelStates)
				}
				next = append(next, n)
			}
		}
		frontier = next
	}
	t.Logf("%d states to depth %d", len(seen), s.depth)
}

func TestEveryOrderOfEventsForTwoJobsInOneStore(t *testing.T) {
	newSearch(1, 10).walk(t)
}

func TestEveryOrderOfEventsForTwoStores(t *testing.T) {
	newSearch(2, 9).walk(t)
}
