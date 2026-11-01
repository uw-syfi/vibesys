//go:build unix

package execution

import (
	"os/exec"
	"syscall"
)

// ownProcessGroup starts the command in a process group of its own and makes a cancelled
// context kill that whole group, so the workers a test runner started die with it instead
// of keeping the captured output open.
func ownProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
}
