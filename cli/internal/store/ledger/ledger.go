// Package ledger is the logic every store.Store shares: what a submission joins or reuses, what a
// transition may change, how plans, events and logs are numbered, and what a store can hold. The
// in-memory and on-disk stores differ only in where a change goes, so they cannot differ in what
// a change is.
//
// A Ledger is pure. It reads and writes no file and takes no lock. Each operation decides: it
// returns the Change the operation amounts to, and the answer, without changing the Ledger. The
// caller makes the Change durable and only then Applies it, so a Change that could not be made
// durable never shows in a read. An Engine runs that sequence behind a lock for a Backend.
//
// Decisions that belong to internal/core stay there: a Ledger asks core.Job.Apply whether a
// transition is legal and what it does, asks core.CanJoin whether a submission joins a job, and
// applies the reuse rule it is given; it does not compute a key.
package ledger

import (
	"fmt"
	"slices"
	"strconv"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// Change is one all-or-nothing change to durable state. Each operation yields at most one.
type Change interface {
	change()
}

// PutJob stores a job, new or replaced. When Result is set, the job is the one it belongs to and
// both become visible together; the result is not visible without the job's new state.
type PutJob struct {
	Job    core.Job
	Result *SavedResult
}

// SavedResult is a result with its place in the order results were saved.
type SavedResult struct {
	Seq    uint64
	Result core.Result
}

// PutPlan stores a plan, new or replaced.
type PutPlan struct{ Plan core.Plan }

// AddPromotion appends a promotion. Seq is its position in the record, from 1.
type AddPromotion struct {
	Seq       uint64
	Promotion core.Promotion
}

// AddEvent appends an event to its run's log.
type AddEvent struct{ Event core.Event }

// AddLog appends bytes to a job's log, which holds Offset bytes before it.
type AddLog struct {
	Job    core.JobID
	Offset uint64
	Data   []byte
}

func (PutJob) change()       {}
func (PutPlan) change()      {}
func (AddPromotion) change() {}
func (AddEvent) change()     {}
func (AddLog) change()       {}

// Snapshot is the state a Ledger is rebuilt from: everything durable except the bodies of events
// and logs, which a Ledger only counts.
type Snapshot struct {
	Jobs       []core.Job
	Results    []SavedResult
	Plans      []core.Plan
	Promotions []core.Promotion
	// Events is the number of events of each run.
	Events map[core.RunID]uint64
	// Logs is the length in bytes of each job's log.
	Logs map[core.JobID]uint64
}

// Ledger is the state of one store. It is not safe for concurrent use.
type Ledger struct {
	rules      store.Rules
	jobs       []core.Job // job number n is jobs[n-1]
	jobIndex   map[core.JobID]int
	byKey      map[core.JobKey][]int // indexes into jobs, ascending
	results    []SavedResult         // by Seq
	resultOf   map[core.JobID]int    // index into results
	plans      []core.Plan           // plan number n is plans[n-1]
	promotions []core.Promotion
	events     map[core.RunID]uint64
	logs       map[core.JobID]uint64
}

// New returns an empty Ledger that applies the rules. A nil rules.Reuse accepts any saved result
// with the submission's key.
func New(rules store.Rules) *Ledger {
	return &Ledger{
		rules:    rules,
		jobIndex: map[core.JobID]int{},
		byKey:    map[core.JobKey][]int{},
		resultOf: map[core.JobID]int{},
		events:   map[core.RunID]uint64{},
		logs:     map[core.JobID]uint64{},
	}
}

func notFound(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf(format, args...)}
}

func conflict(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(format, args...)}
}

func internal(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf(format, args...)}
}

// JobIDFor is the id of job number n, counting from 1.
func JobIDFor(n uint64) core.JobID { return core.JobID("j" + strconv.FormatUint(n, 10)) }

// JobNumber is the number in a job id, and whether id has the form JobIDFor makes.
func JobNumber(id core.JobID) (uint64, bool) {
	rest, ok := strings.CutPrefix(string(id), "j")
	if !ok {
		return 0, false
	}
	n, err := strconv.ParseUint(rest, 10, 64)
	if err != nil || n == 0 || JobIDFor(n) != id {
		return 0, false
	}
	return n, true
}

// Submit decides a submission: the oldest queued or running job core.CanJoin accepts is joined,
// then a saved result the reuse rule accepts is reused, and otherwise a job is created. Joining and
// reuse are skipped for a command job and under NoCache. The note and plan link are appended to
// the job it lands on, and a landing that adds nothing yields a nil Change.
func (l *Ledger) Submit(sub core.Submission) (Change, core.SubmitOutcome, error) {
	var none core.SubmitOutcome
	sub, err := Hold("submission", sub)
	if err != nil {
		return nil, none, err
	}
	if sub.Spec.Settings == nil {
		return nil, none, invalid("submission.Spec.Settings", "is missing")
	}
	if sub.Spec.Commit == "" {
		return nil, none, invalid("submission.Spec.Commit", "is empty")
	}
	kind := sub.Spec.Settings.Kind()
	if kind == core.JobCommand {
		if sub.Key != "" {
			return nil, none, invalid("submission.Key", "a command job has no key")
		}
	} else if err := CheckName("submission.Key", string(sub.Key)); err != nil {
		return nil, none, err
	}
	if sub.Plan != 0 && !l.hasPlan(sub.Plan) {
		return nil, none, notFound("plan %d", sub.Plan)
	}
	if kind != core.JobCommand && !sub.NoCache {
		if i, ok := l.joinable(sub); ok {
			return landOn(l.jobs[i], sub, core.SubmitJoined)
		}
		if i, ok := l.reusable(sub); ok {
			return landOn(l.jobs[i], sub, core.SubmitReused)
		}
	}
	job, _ := attach(core.Job{
		ID:          JobIDFor(uint64(len(l.jobs)) + 1),
		Spec:        sub.Spec,
		State:       core.JobQueued,
		Key:         sub.Key,
		SubmittedAt: sub.At,
	}, sub)
	return PutJob{Job: job}, core.SubmitOutcome{Job: Copy(job), Disposition: core.SubmitCreated}, nil
}

// landOn appends the submission's note and plan link to an existing job. A landing that adds
// nothing yields a nil Change.
func landOn(job core.Job, sub core.Submission, how core.SubmitDisposition) (Change, core.SubmitOutcome, error) {
	job, changed := attach(job, sub)
	out := core.SubmitOutcome{Job: Copy(job), Disposition: how}
	if !changed {
		return nil, out, nil
	}
	return PutJob{Job: job}, out, nil
}

// attach appends the submission's note and plan link to a copy of the job, and says whether
// that changed it. A plan the job already links to is not linked twice.
func attach(job core.Job, sub core.Submission) (core.Job, bool) {
	job = Copy(job)
	changed := false
	if sub.Note != nil {
		job.Notes = append(job.Notes, *sub.Note)
		changed = true
	}
	if sub.Plan != 0 && !slices.Contains(job.Plans, sub.Plan) {
		job.Plans = append(job.Plans, sub.Plan)
		changed = true
	}
	return job, changed
}

// joinable finds the oldest job the submission may join: core.CanJoin decides, because a job in
// flight with the key answers a submission only if it takes as many samples as the submission
// asks for.
func (l *Ledger) joinable(sub core.Submission) (int, bool) {
	for _, i := range l.byKey[sub.Key] {
		if core.CanJoin(l.jobs[i], sub, l.rules.DefaultSamples) {
			return i, true
		}
	}
	return 0, false
}

// reusable finds the job a submission lands on from the saved results with its key, which the reuse
// rule decides over together. Only a job that succeeded has a result, so no state is checked here.
func (l *Ledger) reusable(sub core.Submission) (int, bool) {
	var found []int // indexes into results
	for _, i := range l.byKey[sub.Key] {
		if r, ok := l.resultOf[l.jobs[i].ID]; ok && l.results[r].Result.Key == sub.Key {
			found = append(found, r)
		}
	}
	if len(found) == 0 {
		return 0, false
	}
	slices.Sort(found)
	job := l.results[found[len(found)-1]].Result.Job
	if l.rules.Reuse != nil {
		saved := make([]core.Result, len(found))
		for i, r := range found {
			saved[i] = Copy(l.results[r].Result)
		}
		var ok bool
		if job, ok = l.rules.Reuse(saved, Copy(sub)); !ok {
			return 0, false
		}
	}
	i, ok := l.jobIndex[job]
	return i, ok
}

// Transition decides a state change. core.Job.Apply decides whether the move is legal and what
// it does, and its failure is the answer: FailureConflict unless the job is in t.From, and
// FailureInvalidInput for a move or an effect the job cannot have. The only thing left to the
// ledger is that a result is saved with the new state, in the order results were saved.
func (l *Ledger) Transition(t core.Transition) (Change, core.Job, error) {
	var none core.Job
	t, err := Hold("transition", t)
	if err != nil {
		return nil, none, err
	}
	i, ok := l.jobIndex[t.Job]
	if !ok {
		return nil, none, notFound("job %s", t.Job)
	}
	job, err := Copy(l.jobs[i]).Apply(t)
	if err != nil {
		return nil, none, err
	}
	change := PutJob{Job: job}
	if t.Result != nil {
		change.Result = &SavedResult{Seq: l.nextResultSeq(), Result: *t.Result}
	}
	return change, Copy(job), nil
}

func (l *Ledger) nextResultSeq() uint64 {
	if len(l.results) == 0 {
		return 1
	}
	return l.results[len(l.results)-1].Seq + 1
}

// AddNote decides appending a note to a job in any state.
func (l *Ledger) AddNote(id core.JobID, note core.Note) (Change, core.Job, error) {
	var none core.Job
	note, err := Hold("note", note)
	if err != nil {
		return nil, none, err
	}
	i, ok := l.jobIndex[id]
	if !ok {
		return nil, none, notFound("job %s", id)
	}
	job := Copy(l.jobs[i])
	job.Notes = append(job.Notes, note)
	return PutJob{Job: job}, Copy(job), nil
}

// RecordPromotion decides appending a promotion to the record.
func (l *Ledger) RecordPromotion(p core.Promotion) (Change, error) {
	p, err := Hold("promotion", p)
	if err != nil {
		return nil, err
	}
	if p.Commit == "" {
		return nil, invalid("promotion.Commit", "is empty")
	}
	return AddPromotion{Seq: uint64(len(l.promotions)) + 1, Promotion: p}, nil
}

// CreatePlan decides storing a new open plan with the next number.
func (l *Ledger) CreatePlan(np core.NewPlan) (Change, core.Plan, error) {
	var none core.Plan
	np, err := Hold("plan", np)
	if err != nil {
		return nil, none, err
	}
	plan := core.Plan{
		Number:    core.PlanNumber(len(l.plans) + 1),
		Title:     np.Title,
		Body:      np.Body,
		Status:    core.PlanOpen,
		CreatedAt: np.At,
		UpdatedAt: np.At,
	}
	return PutPlan{Plan: plan}, Copy(plan), nil
}

// EditPlan decides an edit as one change: the fields it sets, the comment it adds and the new
// update time.
func (l *Ledger) EditPlan(number core.PlanNumber, edit core.PlanEdit) (Change, core.Plan, error) {
	var none core.Plan
	edit, err := Hold("edit", edit)
	if err != nil {
		return nil, none, err
	}
	if !l.hasPlan(number) {
		return nil, none, notFound("plan %d", number)
	}
	plan := Copy(l.plans[number-1])
	if edit.Title != nil {
		plan.Title = *edit.Title
	}
	if edit.Body != nil {
		plan.Body = *edit.Body
	}
	if edit.Status != nil {
		plan.Status = *edit.Status
	}
	if edit.Comment != nil {
		plan.Comments = append(plan.Comments, *edit.Comment)
	}
	plan.UpdatedAt = edit.At
	return PutPlan{Plan: plan}, Copy(plan), nil
}

// AppendEvent decides giving the draft the next position in its run's log.
func (l *Ledger) AppendEvent(draft core.EventDraft) (Change, core.Event, error) {
	var none core.Event
	draft, err := Hold("event", draft)
	if err != nil {
		return nil, none, err
	}
	if err := CheckName("event.Run", string(draft.Run)); err != nil {
		return nil, none, err
	}
	event := core.Event{Sequence: l.events[draft.Run] + 1, EventDraft: draft}
	return AddEvent{Event: event}, Copy(event), nil
}

// AppendLog decides appending bytes to a job's log. Appending no bytes changes nothing.
func (l *Ledger) AppendLog(id core.JobID, data []byte) (Change, error) {
	if _, ok := l.jobIndex[id]; !ok {
		return nil, notFound("job %s", id)
	}
	if len(data) == 0 {
		return nil, nil
	}
	return AddLog{Job: id, Offset: l.logs[id], Data: slices.Clone(data)}, nil
}

// Apply makes a Change that has been made durable visible. A nil Change is nothing.
func (l *Ledger) Apply(c Change) {
	switch c := c.(type) {
	case nil:
	case PutJob:
		l.putJob(Copy(c.Job))
		if c.Result != nil {
			saved := Copy(*c.Result)
			l.results = append(l.results, saved)
			l.resultOf[saved.Result.Job] = len(l.results) - 1
		}
	case PutPlan:
		plan := Copy(c.Plan)
		if int(plan.Number) > len(l.plans) {
			l.plans = append(l.plans, plan)
		} else {
			l.plans[plan.Number-1] = plan
		}
	case AddPromotion:
		l.promotions = append(l.promotions, Copy(c.Promotion))
	case AddEvent:
		l.events[c.Event.Run]++
	case AddLog:
		l.logs[c.Job] += uint64(len(c.Data))
	default:
		panic(fmt.Sprintf("ledger: unknown change %T", c))
	}
}

func (l *Ledger) putJob(job core.Job) {
	n, _ := JobNumber(job.ID)
	if int(n) <= len(l.jobs) {
		l.jobs[n-1] = job
		return
	}
	l.jobs = append(l.jobs, job)
	l.jobIndex[job.ID] = len(l.jobs) - 1
	if job.Key != "" {
		l.byKey[job.Key] = append(l.byKey[job.Key], len(l.jobs)-1)
	}
}

func (l *Ledger) hasPlan(n core.PlanNumber) bool { return n >= 1 && int(n) <= len(l.plans) }

// Job returns one job.
func (l *Ledger) Job(id core.JobID) (core.Job, error) {
	i, ok := l.jobIndex[id]
	if !ok {
		return core.Job{}, notFound("job %s", id)
	}
	return Copy(l.jobs[i]), nil
}

// Jobs returns the jobs matching the filter, oldest submission first.
func (l *Ledger) Jobs(f core.JobFilter) []core.Job {
	var out []core.Job
	for _, job := range l.jobs {
		if matchesJob(f, job) {
			out = append(out, Copy(job))
		}
	}
	return out
}

func matchesJob(f core.JobFilter, job core.Job) bool {
	switch {
	case f.Commit != "" && job.Spec.Commit != f.Commit:
		return false
	case len(f.States) > 0 && !slices.Contains(f.States, job.State):
		return false
	case f.Kind != "" && job.Spec.Settings.Kind() != f.Kind:
		return false
	case f.NoteContains != "" && !slices.ContainsFunc(job.Notes, func(n core.Note) bool {
		return strings.Contains(n.Text, f.NoteContains)
	}):
		return false
	}
	return true
}

// Results returns the saved results matching the filter, in the order they were saved.
func (l *Ledger) Results(f core.ResultFilter) []core.Result {
	var out []core.Result
	for _, saved := range l.results {
		r := saved.Result
		if (f.Commit == "" || r.Commit == f.Commit) && (f.Key == "" || r.Key == f.Key) {
			out = append(out, Copy(r))
		}
	}
	return out
}

// Promotions returns every recorded promotion, oldest first.
func (l *Ledger) Promotions() []core.Promotion { return Copy(l.promotions) }

// Plan returns one plan.
func (l *Ledger) Plan(n core.PlanNumber) (core.Plan, error) {
	if !l.hasPlan(n) {
		return core.Plan{}, notFound("plan %d", n)
	}
	return Copy(l.plans[n-1]), nil
}

// Plans returns the plans matching the filter, by number.
func (l *Ledger) Plans(f core.PlanFilter) []core.Plan {
	var out []core.Plan
	for _, plan := range l.plans {
		if len(f.Statuses) > 0 && !slices.Contains(f.Statuses, plan.Status) {
			continue
		}
		if f.Text != "" && !strings.Contains(plan.Title, f.Text) && !strings.Contains(plan.Body, f.Text) {
			continue
		}
		out = append(out, Copy(plan))
	}
	return out
}

// EventWindow is the positions of a run's events a read asks for: first to last, both included.
// There are none when first is greater than last.
func (l *Ledger) EventWindow(run core.RunID, after uint64, limit int) (first, last uint64, err error) {
	if limit < 0 {
		return 0, 0, invalid("limit", fmt.Sprintf("%d is negative", limit))
	}
	total := l.events[run]
	if after >= total {
		return 1, 0, nil
	}
	first, last = after+1, total
	if limit > 0 && last-first+1 > uint64(limit) {
		last = first + uint64(limit) - 1
	}
	return first, last, nil
}

// LogWindow is the bytes of a job's log a read asks for: from to to, with to excluded.
func (l *Ledger) LogWindow(id core.JobID, offset uint64, limit int) (from, to uint64, err error) {
	if _, ok := l.jobIndex[id]; !ok {
		return 0, 0, notFound("log of job %s", id)
	}
	if limit < 0 {
		return 0, 0, invalid("max", fmt.Sprintf("%d is negative", limit))
	}
	length := l.logs[id]
	if offset > length {
		return 0, 0, invalid("offset", fmt.Sprintf("%d is past the end of the log, at %d", offset, length))
	}
	to = length
	if limit > 0 && length-offset > uint64(limit) {
		to = offset + uint64(limit)
	}
	return offset, to, nil
}
