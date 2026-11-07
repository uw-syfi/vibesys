package daemontest

import (
	"context"
	"sync"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// JobLog is a Fake of the job service's log of a task. Results are recorded in it as they happen. The host
// has a kept view of it that follows a stream and can be behind (a test puts that into a search.Store when it
// chooses), while a read made now, which is what ReadJobLog is, sees every result recorded before it.
type JobLog struct {
	mu    sync.Mutex
	facts search.Facts
	// Reads counts the reads made.
	Reads int
	// Err, when set, is what a read fails with, and Reason, when set, is why the view it returns cannot
	// be read.
	Err    error
	Reason core.ViewReason
}

// Record adds facts to the log, as the job service does when a job ends.
func (l *JobLog) Record(facts search.Facts) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.facts = append(l.facts, facts...)
}

// ReadJobLog implements daemon.JobLog.
func (l *JobLog) ReadJobLog(ctx context.Context, _ core.Run) (search.View, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.Reads++
	if l.Err != nil {
		return search.View{}, l.Err
	}
	view := search.Fold(l.facts)
	if l.Reason != core.Readable {
		view.Reason = l.Reason
	}
	return view, nil
}

// Held is what has been recorded, as a kept view would hold it if it had every fact.
func (l *JobLog) Held() search.View {
	l.mu.Lock()
	defer l.mu.Unlock()
	return search.Fold(l.facts)
}
