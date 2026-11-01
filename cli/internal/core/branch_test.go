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
