package filestore

import (
	"fmt"
	"path"
	"strconv"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// The names below are the whole physical layout. Nothing outside this package spells them.
const (
	stateDirName = ".vibesys-state"

	formatFile = "format"

	jobsDir       = "jobs"
	resultsDir    = "results"
	plansDir      = "plans"
	promotionsDir = "promotions"
	runsDir       = "runs"
	logsDir       = "logs"

	recordSuffix = ".json"
	tempPrefix   = ".tmp-"
)

// topDirs are the directories of an initialized store, in the order they are made.
var topDirs = []string{jobsDir, resultsDir, plansDir, promotionsDir, runsDir, logsDir}

func jobPath(root string, id core.JobID) string {
	return path.Join(root, jobsDir, string(id)+recordSuffix)
}

func resultPath(root string, id core.JobID) string {
	return path.Join(root, resultsDir, string(id)+recordSuffix)
}

func planPath(root string, n core.PlanNumber) string {
	return path.Join(root, plansDir, strconv.FormatUint(uint64(n), 10)+recordSuffix)
}

func promotionPath(root string, seq uint64) string {
	return path.Join(root, promotionsDir, strconv.FormatUint(seq, 10)+recordSuffix)
}

func eventPath(root string, run core.RunID, seq uint64) string {
	return path.Join(root, runsDir, string(run), strconv.FormatUint(seq, 10)+recordSuffix)
}

// chunkName is the name of the log chunk that starts at offset. It is padded so that names sort
// in offset order.
func chunkName(offset uint64) string { return fmt.Sprintf("%020d", offset) }

func chunkPath(root string, job core.JobID, offset uint64) string {
	return path.Join(root, logsDir, string(job), chunkName(offset))
}

// canonicalNumber parses the decimal form that strconv.FormatUint writes: no sign, no leading
// zeros, not zero.
func canonicalNumber(s string) (uint64, bool) {
	n, err := strconv.ParseUint(s, 10, 64)
	if err != nil || n == 0 || strconv.FormatUint(n, 10) != s {
		return 0, false
	}
	return n, true
}

// recordName strips the record suffix from a file name.
func recordName(file string) (string, bool) { return strings.CutSuffix(file, recordSuffix) }

// hidden says a name belongs to the machinery around the store, not to its records: the temp
// files a commit writes before it renames them, and what a network filesystem leaves behind.
func hidden(name string) bool { return strings.HasPrefix(name, ".") }

// taskDir is where a repository keeps the state of one task.
func taskDir(repository, task string) string { return path.Join(repository, stateDirName, task) }
