package terminal

import "golang.org/x/sys/unix"

// The requests that read and set a terminal's settings.
const (
	getTermios = unix.TCGETS
	setTermios = unix.TCSETS
)

// disableSuspend turns off the key that stops the program (Ctrl-Z). A program stopped in key mode leaves
// the shell in it, and nothing puts the view's mode back when the program continues.
func disableSuspend(t *unix.Termios) {
	t.Cc[unix.VSUSP] = 0 // _POSIX_VDISABLE
}
