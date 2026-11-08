package proctable

import (
	"slices"
	"testing"
	"testing/quick"
)

// test-isolation: marked chooses which processes a sweep kills, over trees no test can start at will:
// a cycle of parents, a parent that is not in the table, and the caller inside the tree. The public
// WithEnvUnder reads the machine's real table, so it cannot be given them.

// Whatever the shape of the table, a process is chosen exactly when it is not the caller and it, or an
// ancestor reached without passing through the caller, is accepted.
func TestMarkedChoosesAcceptedProcessesAndTheirDescendantsExceptTheCaller(t *testing.T) {
	err := quick.Check(func(parents []uint8, accepted []bool, selfAt uint8) bool {
		n := len(parents)
		if n == 0 || n > 40 {
			return true
		}
		procs := make([]process, n)
		parentOf := map[int]int{}
		isAccepted := map[int]bool{}
		for i := range procs {
			pid := i + 1
			// A parent among the table's pids, or one that is not in it.
			ppid := int(parents[i]) % (n + 3)
			procs[i] = process{pid: pid, ppid: ppid}
			if i < len(accepted) && accepted[i] {
				procs[i].environ = []string{"MARK=1"}
			}
			parentOf[pid], isAccepted[pid] = ppid, procs[i].environ != nil
		}
		// 0 is a caller that is not in the table.
		self := int(selfAt) % (n + 1)
		var chosen func(pid int, seen map[int]bool) bool
		chosen = func(pid int, seen map[int]bool) bool {
			if pid == self || seen[pid] {
				return false
			}
			seen[pid] = true
			if _, inTable := parentOf[pid]; !inTable {
				return false
			}
			return isAccepted[pid] || chosen(parentOf[pid], seen)
		}
		var want []int
		for _, p := range procs {
			if chosen(p.pid, map[int]bool{}) {
				want = append(want, p.pid)
			}
		}
		got := marked(procs, self, func(environ []string) bool { return len(environ) > 0 })
		return slices.Equal(got, want)
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
}
