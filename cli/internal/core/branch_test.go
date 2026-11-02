package core_test

import (
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// gitRefOK states the rules git applies to a branch name, as far as a name made of the characters
// a task may use can break them.
func gitRefOK(name string) bool {
	if strings.Contains(name, "..") || strings.HasSuffix(name, ".") || strings.HasSuffix(name, ".lock") {
		return false
	}
	for _, component := range strings.Split(name, "/") {
		if component == "" || strings.HasPrefix(component, ".") || strings.HasSuffix(component, ".lock") {
			return false
		}
	}
	return true
}

// A branch is named for every task name that can be a git branch, and for no other, and a branch
// that is named is always one git accepts.
func TestResultBranchIsAValidRefForExactlyTheTasksThatCanHaveOne(t *testing.T) {
	alphabet := "ab0.-_Z"
	property := func(picks []uint8) bool {
		var b strings.Builder
		for _, p := range picks {
			b.WriteByte(alphabet[int(p)%len(alphabet)])
		}
		task := b.String()
		if strings.HasPrefix(task, ".") {
			return true // a scope never carries one
		}
		got, err := core.ResultBranch(task)
		if task == "" || !gitRefOK(core.ResultBranchPrefix+task) {
			return err != nil && failureKind(err) == core.FailureInvalidInput && strings.Contains(err.Error(), "task")
		}
		return err == nil && got == core.ResultBranchPrefix+task && gitRefOK(got)
	}
	if err := quick.Check(property, &quick.Config{MaxCount: properties}); err != nil {
		t.Fatal(err)
	}
}

func TestResultBranchNamesAPlainTask(t *testing.T) {
	got, err := core.ResultBranch("kv-store")
	if err != nil || got != "vibesys/result/kv-store" {
		t.Fatalf("ResultBranch = %q, %v", got, err)
	}
}

// FuzzResultBranch feeds the task-to-branch rule any bytes. It never panics, fails only with an
// invalid-input failure that names the task, and when it names a branch the branch is the prefix
// and the task and obeys the ref rules it states.
func FuzzResultBranch(f *testing.F) {
	for _, seed := range []string{"", "kv-store", "a..b", "a.", "x.lock", ".hidden", "a/b", "a b", "名前", "a\x00b", strings.Repeat("a", 300)} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, task string) {
		got, err := core.ResultBranch(task)
		if err != nil {
			if failureKind(err) != core.FailureInvalidInput || !strings.Contains(err.Error(), "task") {
				t.Fatalf("ResultBranch(%q) failed with %v, want an invalid-input failure naming the task", task, err)
			}
			return
		}
		if got != core.ResultBranchPrefix+task {
			t.Fatalf("ResultBranch(%q) = %q", task, got)
		}
		if strings.Contains(task, "..") || strings.HasSuffix(task, ".") || strings.HasSuffix(task, ".lock") {
			t.Fatalf("ResultBranch(%q) named a branch git refuses", task)
		}
	})
}
