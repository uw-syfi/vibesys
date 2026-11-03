// Package search derives in-memory search views from committed CLI task facts.
// The CLI remains the source of truth; caps apply only to presentation, never notifications.
package search

import (
	"context"
	"sort"
	"sync"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"google.golang.org/protobuf/proto"
)

// Reason describes whether the retained view can currently be refreshed.
type Reason uint8

const (
	Readable Reason = iota
	NoDaemon
	StartFailed
	CLINotInstalled
	ClientTooOld
	DaemonTooOld
	StreamLost
	StoreOwnedElsewhere
	StoreAway
)

// View owns its facts. Unreadable retains facts and records a typed link failure.
type View struct {
	Facts            []*pb.TaskFact
	Reason           Reason
	Detail           string
	Truncated        bool
	UnattributedJobs uint32
}

// Fold replaces a view from one committed Session result. It never aliases input.
func Fold(facts []*pb.TaskFact) View {
	v := View{}
	for _, f := range facts {
		v.Facts = append(v.Facts, proto.Clone(f).(*pb.TaskFact))
		if away := f.GetHealth().GetAway(); away != nil {
			v.Reason = StoreAway
			if away.Reason == pb.AwayReason_AWAY_REASON_OWNED_ELSEWHERE {
				v.Reason = StoreOwnedElsewhere
			}
			v.Detail = away.GetFailure().GetMessage()
		}
		if j := f.GetJob(); j != nil {
			attributed := false
			for _, s := range j.Submitters {
				attributed = attributed || s.RunId != ""
			}
			if !attributed {
				v.UnattributedJobs++
			}
		}
	}
	return v
}

// Cap returns the display window: newest 500 jobs and their results, 100 judgments,
// and newest 200 plans and promotions. Full facts stay available to Source.
func Cap(v View) View {
	out := Fold(v.Facts)
	out.Reason, out.Detail = v.Reason, v.Detail
	out.Truncated, out.UnattributedJobs = v.Truncated, v.UnattributedJobs
	sort.SliceStable(out.Facts, func(i, j int) bool {
		a, b := out.Facts[i], out.Facts[j]
		if a.GetJob() != nil && b.GetJob() != nil {
			x, y := a.GetJob(), b.GetJob()
			if x.GetSubmittedAt().AsTime() != y.GetSubmittedAt().AsTime() {
				return x.GetSubmittedAt().AsTime().After(y.GetSubmittedAt().AsTime())
			}
			return x.Id > y.Id
		}
		if a.GetPlan() != nil && b.GetPlan() != nil {
			return a.GetPlan().Number > b.GetPlan().Number
		}
		if a.GetPromotion() != nil && b.GetPromotion() != nil {
			return a.GetPromotion().Seq > b.GetPromotion().Seq
		}
		return factOrder(a) < factOrder(b)
	})
	counts := map[int]int{}
	jobs := map[string]bool{}
	var kept []*pb.TaskFact
	for _, f := range out.Facts {
		kind := factOrder(f)
		limit := 0
		switch kind {
		case 0:
			limit = 500
		case 2:
			limit = 100
		case 3, 4:
			limit = 200
		}
		counts[kind]++
		if limit > 0 && counts[kind] > limit {
			out.Truncated = true
			continue
		}
		if j := f.GetJob(); j != nil {
			jobs[j.Id] = true
		}
		if r := f.GetResult(); r != nil && !jobs[r.JobId] {
			continue
		}
		kept = append(kept, f)
	}
	out.Facts = kept
	return out
}
func factOrder(f *pb.TaskFact) int {
	switch f.GetFact().(type) {
	case *pb.TaskFact_Job:
		return 0
	case *pb.TaskFact_Result:
		return 1
	case *pb.TaskFact_Judgment:
		return 2
	case *pb.TaskFact_Promotion:
		return 3
	case *pb.TaskFact_Plan:
		return 4
	default:
		return 5
	}
}

// Store owns each run's full view and a boot-local monotonic revision.
type Store struct {
	mu       sync.Mutex
	views    map[core.RunID]entry
	revision uint64
}
type entry struct {
	view     View
	revision uint64
}

func (s *Store) Put(run core.RunID, v View) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.views == nil {
		s.views = map[core.RunID]entry{}
	}
	old, ok := s.views[run]
	if ok && old.view.Reason == v.Reason && old.view.Detail == v.Detail && equalFacts(old.view.Facts, v.Facts) {
		return
	}
	s.revision++
	c := Fold(v.Facts)
	c.Reason, c.Detail = v.Reason, v.Detail
	s.views[run] = entry{c, s.revision}
}
func (s *Store) Get(run core.RunID) (View, uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e := s.views[run]
	v := Fold(e.view.Facts)
	v.Reason, v.Detail = e.view.Reason, e.view.Detail
	return v, e.revision
}
func (s *Store) Delete(run core.RunID) { s.mu.Lock(); defer s.mu.Unlock(); delete(s.views, run) }

// Source implements jobs.JobSource over full local views, including stale ones.
type Source struct{ Store *Store }

func (s Source) Jobs(ctx context.Context, run core.Run) ([]core.JobUpdate, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	v, _ := s.Store.Get(run.ID)
	var out []core.JobUpdate
	for _, f := range v.Facts {
		j := f.GetJob()
		if j == nil {
			continue
		}
		var state core.JobState
		switch j.State {
		case pb.JobState_JOB_STATE_QUEUED:
			state = core.JobQueued
		case pb.JobState_JOB_STATE_RUNNING:
			state = core.JobRunning
		case pb.JobState_JOB_STATE_SUCCEEDED:
			state = core.JobSucceeded
		case pb.JobState_JOB_STATE_FAILED:
			state = core.JobFailed
		case pb.JobState_JOB_STATE_CANCELLED:
			state = core.JobCancelled
		case pb.JobState_JOB_STATE_LOST:
			state = core.JobLost
		default:
			continue
		}
		for _, sub := range j.Submitters {
			if sub.RunId != string(run.ID) {
				continue
			}
			detail := ""
			if state == core.JobCancelled {
				detail = j.CancelReason
			}
			out = append(out, core.JobUpdate{Run: run.ID, Job: core.JobID(j.Id), State: state, SubmittedBy: core.AgentID(sub.Agent), Detail: detail})
			break
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Job < out[j].Job })
	return out, nil
}

func equalFacts(a, b Facts) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if !proto.Equal(a[i], b[i]) {
			return false
		}
	}
	return true
}
