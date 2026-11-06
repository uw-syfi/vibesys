package terminal

import "golang.org/x/sys/unix"

// The requests that read and set a terminal's settings.
const (
	getTermios = unix.TIOCGETA
	setTermios = unix.TIOCSETA
)
