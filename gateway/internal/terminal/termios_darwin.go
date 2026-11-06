package terminal

import "golang.org/x/sys/unix"

// The requests that read and set a terminal's settings.
const (
	getTermios = unix.TIOCGETA
	setTermios = unix.TIOCSETA
)

// disableSuspend turns off the keys that stop the program (Ctrl-Z, and Ctrl-Y which stops it when it
// next reads). A program stopped in key mode leaves the shell in it, and nothing puts the view's mode
// back when the program continues.
func disableSuspend(t *unix.Termios) {
	t.Cc[unix.VSUSP] = 0xff // _POSIX_VDISABLE
	t.Cc[unix.VDSUSP] = 0xff
}
