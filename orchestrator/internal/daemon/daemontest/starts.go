package daemontest

import (
	"sync"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// StartCall is one question put to StartChecks.
type StartCall struct{ Repository, Task string }

// StartChecks is a daemon.StartChecks that accepts every start until a test makes it refuse, and
// remembers what it was asked.
type StartChecks struct {
	mu      sync.Mutex
	refusal *daemon.StartRefusal
	calls   []StartCall
}

// Refuse makes every later check answer with refusal. Nil accepts again.
func (s *StartChecks) Refuse(refusal *daemon.StartRefusal) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.refusal = refusal
}

// Calls lists the checks made so far, in order.
func (s *StartChecks) Calls() []StartCall {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]StartCall(nil), s.calls...)
}

// CheckStart implements daemon.StartChecks.
func (s *StartChecks) CheckStart(repository, task string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls = append(s.calls, StartCall{repository, task})
	if s.refusal != nil {
		return s.refusal
	}
	return nil
}
