package core_test

import (
	"errors"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A task name is one path element: never empty, ".", "..", or anything with a separator. Every
// string is either refused with a failure naming the field, or accepted as itself.
func TestATaskNameIsOnePathElementOrRefused(t *testing.T) {
	property := func(s string) bool {
		name, err := core.ParseTaskName("context.task", s)
		if err != nil {
			var f *core.Failure
			return errors.As(err, &f) && f.Kind == core.FailureInvalidInput && strings.HasPrefix(f.Message, "context.task: ")
		}
		return string(name) == s && s != "" && s != "." && s != ".." && !strings.ContainsAny(s, `/\`) &&
			filepath.Base(s) == s && !strings.HasPrefix(s, ".") && len(s) <= 128
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 2000}); err != nil {
		t.Fatal(err)
	}
	for _, good := range []string{"kv-store", "task", "a.b_c-1", strings.Repeat("x", 128)} {
		if _, err := core.ParseTaskName("task", good); err != nil {
			t.Errorf("%q: %v", good, err)
		}
	}
	for _, bad := range []string{"", ".", "..", ".hidden", "a/b", "../escape", strings.Repeat("x", 129), "sp ace"} {
		if _, err := core.ParseTaskName("task", bad); err == nil {
			t.Errorf("%q was accepted", bad)
		}
	}
}

// A scope holds a clean absolute repository and a task name, and nothing else: whatever strings
// it is given, it refuses them or keeps them exactly.
func TestAScopeIsACleanAbsoluteRepositoryAndATaskName(t *testing.T) {
	task := regexp.MustCompile(`^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$`)
	property := func(repo, name string) bool {
		for _, r := range []string{repo, "/" + repo} {
			s, err := core.ResolvedScope(core.Repository(r), core.TaskName(name))
			valid := filepath.IsAbs(r) && filepath.Clean(r) == r && task.MatchString(name)
			if (err == nil) != valid {
				return false
			}
			if err == nil && (string(s.Repository()) != r || string(s.Task()) != name) {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 2000}); err != nil {
		t.Fatal(err)
	}
	if _, err := core.ResolvedScope("/repo/../repo", "task"); err == nil {
		t.Fatal("a path that is not clean made a scope")
	}
	if s, err := core.ResolvedScope("/repo", "task"); err != nil || s == (core.Scope{}) {
		t.Fatalf("a valid scope: %v, %v", s, err)
	}
}
