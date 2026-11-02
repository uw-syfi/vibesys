package lifecycle

import (
	"crypto/sha256"
	"encoding/hex"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// StoreID names the store of one task in one repository, as a short string that is safe in a file
// name. It is a digest of the two, so the same pair always names the same store.
type StoreID string

// StoreIDOf returns the id of the store of a task in a repository.
func StoreIDOf(repository, task string) StoreID {
	sum := sha256.Sum256([]byte(repository + "\x00" + task))
	return StoreID(hex.EncodeToString(sum[:6]))
}

// Ref is a job's identity across every store the daemon serves: job ids are numbered per store,
// so two stores both have a "j1".
type Ref struct {
	Store StoreID
	Job   core.JobID
}

// String names the job for a log line and for the name of its run directory.
func (r Ref) String() string { return string(r.Store) + "-" + string(r.Job) }
