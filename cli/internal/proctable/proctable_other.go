//go:build !linux && !darwin

package proctable

import (
	"fmt"
	"runtime"
)

// A system with neither /proc nor the sysctl of macOS has no reader here, and says so; it does not
// answer with an empty table.

func table() ([]process, error) {
	return nil, fmt.Errorf("%w (%s)", ErrUnsupported, runtime.GOOS)
}

func running(int) (bool, error) {
	return false, fmt.Errorf("%w (%s)", ErrUnsupported, runtime.GOOS)
}
