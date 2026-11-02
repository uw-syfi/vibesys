package core

import (
	"fmt"
	"path/filepath"
	"regexp"
)

// TaskName names a task: the directory .vibesys/tasks/<name>/ of a repository. A TaskName is made
// by ParseTaskName, so code past the boundary never checks one again.
type TaskName string

// taskNamePattern is what a task directory may be called: 1 to 128 of A-Z, a-z, 0-9, dot,
// underscore and hyphen, not starting with a dot, so a name is one path element and never "." or
// "..". The store's names follow the same rule.
var taskNamePattern = regexp.MustCompile(`^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$`)

// ParseTaskName returns the task name s, or a failure of kind invalid input that names field.
func ParseTaskName(field, s string) (TaskName, error) {
	if !taskNamePattern.MatchString(s) {
		return "", failf(FailureInvalidInput,
			"%s: %q is not a task name (1 to 128 of A-Z, a-z, 0-9, dot, underscore and hyphen, not starting with a dot)", field, s)
	}
	return TaskName(s), nil
}

// Repository is the canonical absolute path of a git repository: the one path every linked
// worktree of it resolves to. A store, a result branch and the task sources are all reached
// through it.
type Repository string

// Scope names the store a request is about: one task in one repository. Its fields are unexported
// and it is made only by ResolvedScope, which archcheck lets only resolution, and the reading back
// of what resolution wrote, name. So a store key is never made from a path a caller sent as it
// was. Scope is comparable and keys maps.
type Scope struct {
	repository Repository
	task       TaskName
}

// ResolvedScope returns the scope of a resolved repository and a parsed task name. It fails with a
// failure of kind invalid input for a repository that is not a clean absolute path, which no
// resolution produces.
func ResolvedScope(repository Repository, task TaskName) (Scope, error) {
	r := string(repository)
	if !filepath.IsAbs(r) || filepath.Clean(r) != r {
		return Scope{}, failf(FailureInvalidInput, "repository: %q is not a clean absolute path", r)
	}
	if _, err := ParseTaskName("task", string(task)); err != nil {
		return Scope{}, err
	}
	return Scope{repository: repository, task: task}, nil
}

// Repository returns the scope's repository.
func (s Scope) Repository() Repository { return s.repository }

// Task returns the scope's task.
func (s Scope) Task() TaskName { return s.task }

// String names the scope for messages.
func (s Scope) String() string { return fmt.Sprintf("task %s in %s", s.task, s.repository) }
