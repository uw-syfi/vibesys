package core

import "strings"

// ResultBranchPrefix is where result branches live: a task's is ResultBranchPrefix + the task.
const ResultBranchPrefix = "vibesys/result/"

// ResultBranch names the git branch that holds the best commit of a task, which `promote` moves
// and `compare` and `promote` measure against. The branch is created at the commit a run starts
// from, so it always has a head, and it moves only forward.
//
// A task name is already limited to letters, digits, dot, underscore and hyphen, and may not
// start with a dot. Git refuses a name with two dots in a row, one that ends in a dot, and a
// component that ends in ".lock", so a task name that has one of those has no branch and fails
// with a *Failure of kind FailureInvalidInput.
func ResultBranch(task string) (string, error) {
	switch {
	case task == "":
		return "", failf(FailureInvalidInput, "task: no task to name a result branch for")
	case strings.Contains(task, ".."), strings.HasSuffix(task, "."), strings.HasSuffix(task, ".lock"):
		return "", failf(FailureInvalidInput,
			"task: %q cannot name a git branch (no \"..\", no trailing dot, no \".lock\" ending); rename the task", task)
	}
	return ResultBranchPrefix + task, nil
}
