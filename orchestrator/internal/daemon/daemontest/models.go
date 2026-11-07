package daemontest

import (
	"sync"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// Models is a daemon.ModelPolicySource a test sets: the host's policy file and the catalog of its
// agents' tool, which a test changes between two looks as an operator edits the file.
type Models struct {
	mu     sync.Mutex
	policy core.ModelPolicy
}

// Set makes the policy what the next look finds.
func (m *Models) Set(policy core.ModelPolicy) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.policy = policy
}

// ModelPolicy implements daemon.ModelPolicySource.
func (m *Models) ModelPolicy() core.ModelPolicy {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.policy
}
