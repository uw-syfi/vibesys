// Package search derives in-memory search views from committed CLI task facts.
// The CLI remains the source of truth.
package search

import (
	"context"
	"sort"
	"sync"

	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
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
	Connecting
	MessageTooLarge
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
	// Entries are immutable after Put; hold the lock while reading rather than
	// cloning every protobuf for each run on each poll.
	s.Store.mu.Lock()
	defer s.Store.mu.Unlock()
	v := s.Store.views[run.ID].view
	submitted := map[core.JobID]int64{}
	var out []core.JobUpdate
	for _, f := range v.Facts {
		j := f.GetJob()
		if j == nil {
			continue
		}
		submitted[core.JobID(j.Id)] = j.GetSubmittedAt().AsTime().UnixNano()
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
	sort.Slice(out, func(i, j int) bool {
		a, b := submitted[out[i].Job], submitted[out[j].Job]
		if a != b {
			return a < b
		}
		return out[i].Job < out[j].Job
	})
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
