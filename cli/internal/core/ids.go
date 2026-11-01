package core

// JobID names one job. The daemon assigns it when it queues the job.
type JobID string

// Commit is a full commit hash, 40 hexadecimal characters. Callers resolve refs before a Commit
// exists, so a Commit always names the exact tree a result came from.
type Commit string

// Valid reports whether the commit is 40 lowercase hexadecimal characters. Two spellings of one
// commit would be two cache keys, so a Commit is lowercase before it is used.
func (c Commit) Valid() bool {
	if len(c) != 40 {
		return false
	}
	for _, r := range c {
		if (r < '0' || r > '9') && (r < 'a' || r > 'f') {
			return false
		}
	}
	return true
}

// JobKey is the cache key of a job: a digest of KeyInputs. Command jobs have none.
type JobKey string

// RunID names one optimization run, the span events and a worktree set belong to.
type RunID string

// PlanNumber is a plan's number, assigned by the store. Zero means no plan.
type PlanNumber uint32

// CallContext identifies who made a call and about what. It fills the envelope of the event the
// call appends.
type CallContext struct {
	// Repository is the absolute path of the git repository the command ran in.
	Repository string
	Task       string
	Run        RunID
	// Harness is the agent harness, such as "claude-code".
	Harness string
	Agent   string
}
