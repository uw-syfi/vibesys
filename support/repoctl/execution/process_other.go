//go:build !unix

package execution

import "os/exec"

// ownProcessGroup is a no-op where process groups are not available.
func ownProcessGroup(*exec.Cmd) {}
