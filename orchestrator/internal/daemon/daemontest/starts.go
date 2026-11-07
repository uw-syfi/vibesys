package daemontest

import (
	"sync"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// StartCall is one question put to StartChecks.
type StartCall struct{ Repository, Task string }

// StartChecks is a daemon.StartChecks that accepts every start until a test makes it refuse, and
// remembers what it was asked. It is a tree of repositories that are all fit to start in, with
// links between their names.
type StartChecks struct {
	mu      sync.Mutex
	refusal *daemon.StartRefusal
	calls   []StartCall
	links   map[string]string
}

// Link makes the repository name from a symbolic link to the repository to, which is what a start
// of from then runs in.
func (s *StartChecks) Link(from, to string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.links == nil {
		s.links = map[string]string{}
	}
	s.links[from] = to
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

// CheckRepository implements daemon.StartChecks. It follows the same refusal and links as
// CheckStart and records its call with an empty task.
func (s *StartChecks) CheckRepository(repository string) (string, error) {
	return s.CheckStart(repository, "")
}

// CheckStart implements daemon.StartChecks.
func (s *StartChecks) CheckStart(repository, task string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls = append(s.calls, StartCall{repository, task})
	if s.refusal != nil {
		return "", s.refusal
	}
	for range len(s.links) + 1 {
		to, linked := s.links[repository]
		if !linked {
			return repository, nil
		}
		repository = to
	}
	return "", &daemon.StartRefusal{Reason: "repository " + repository + " is a link that leads back to itself"}
}
