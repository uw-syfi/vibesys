//go:build e2e && !linux

package e2e

import "runtime"

// Where there is no /proc the leak checks cannot see the machine's processes, so a test that reaches
// one stops with the reason and does not pass on a list that is empty because it was never read (see
// processes_linux_test.go). Run this tier on Linux.

func noProcessTable() {
	panic("gateway/e2e: this test checks the live processes through /proc, which " + runtime.GOOS +
		" does not have, so it would pass without looking; run the e2e tier on Linux")
}

func processesMarked(string) []int {
	noProcessTable()
	return nil
}

func processTable() []proc {
	noProcessTable()
	return nil
}

func processesOf(string) []int {
	noProcessTable()
	return nil
}
