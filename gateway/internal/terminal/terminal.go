// Package terminal is the real terminal behind the keyed view of `vibesys-client run attach`: it puts
// the terminal in the mode that delivers single key presses, puts it back, says how big it is, and says
// when that changes. It decides nothing; the view is in orchestrator/clientcli, which reads this
// through its Screen interface.
package terminal

import (
	"os"
	"os/signal"
	"sync"
	"syscall"

	"golang.org/x/sys/unix"
)

// Terminal is the terminal that in reads from and out writes to.
type Terminal struct {
	in, out *os.File
	once    sync.Once
	resizes chan struct{}
}

// New is the terminal of the two files, which are the terminal's input and output.
func New(in, out *os.File) *Terminal {
	return &Terminal{in: in, out: out}
}

// Raw delivers each key press at once and without echo, and keeps what the terminal does with Ctrl-C
// (it still raises an interrupt) and with output (a line break still returns the cursor). The function
// it returns puts every setting back as it was found, and may be called more than once.
func (t *Terminal) Raw() (func(), error) {
	fd := int(t.in.Fd())
	saved, err := unix.IoctlGetTermios(fd, getTermios)
	if err != nil {
		return nil, err
	}
	keys := *saved
	keys.Lflag &^= unix.ICANON | unix.ECHO
	keys.Cc[unix.VMIN] = 1
	keys.Cc[unix.VTIME] = 0
	if err := unix.IoctlSetTermios(fd, setTermios, &keys); err != nil {
		return nil, err
	}
	var once sync.Once
	return func() { once.Do(func() { _ = unix.IoctlSetTermios(fd, setTermios, saved) }) }, nil
}

// Size is the width and height of the terminal, or zeros when it cannot be read.
func (t *Terminal) Size() (int, int) {
	size, err := unix.IoctlGetWinsize(int(t.out.Fd()), unix.TIOCGWINSZ)
	if err != nil {
		return 0, 0
	}
	return int(size.Col), int(size.Row)
}

// Resizes receives once for each time the terminal changed size, as the process is told by SIGWINCH.
func (t *Terminal) Resizes() <-chan struct{} {
	t.once.Do(func() {
		t.resizes = make(chan struct{}, 1)
		signals := make(chan os.Signal, 1)
		signal.Notify(signals, syscall.SIGWINCH)
		go func() {
			for range signals {
				select {
				case t.resizes <- struct{}{}:
				default: // one is already waiting to be read, and says all there is to say.
				}
			}
		}()
	})
	return t.resizes
}
