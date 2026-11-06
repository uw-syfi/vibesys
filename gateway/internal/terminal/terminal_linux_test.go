package terminal_test

import (
	"fmt"
	"os"
	"testing"

	"golang.org/x/sys/unix"

	"github.com/uw-syfi/vibesys/gateway/internal/terminal"
)

// pty opens a pseudo-terminal pair: the master a test drives, and the slave a program would be on.
func pty(t *testing.T) (master, slave *os.File) {
	t.Helper()
	master, err := os.OpenFile("/dev/ptmx", os.O_RDWR|unix.O_NOCTTY, 0)
	if err != nil {
		t.Skipf("no pseudo-terminals here: %v", err)
	}
	t.Cleanup(func() { master.Close() })
	if err := unix.IoctlSetPointerInt(int(master.Fd()), unix.TIOCSPTLCK, 0); err != nil {
		t.Fatal(err)
	}
	n, err := unix.IoctlGetInt(int(master.Fd()), unix.TIOCGPTN)
	if err != nil {
		t.Fatal(err)
	}
	slave, err = os.OpenFile(fmt.Sprintf("/dev/pts/%d", n), os.O_RDWR|unix.O_NOCTTY, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { slave.Close() })
	return master, slave
}

func settings(t *testing.T, f *os.File) unix.Termios {
	t.Helper()
	s, err := unix.IoctlGetTermios(int(f.Fd()), unix.TCGETS)
	if err != nil {
		t.Fatal(err)
	}
	return *s
}

// Raw takes the terminal out of line mode and off echo, keeps the interrupt key and the output
// processing, and its restore puts every setting back, however often it is called.
func TestRawModeIsRestoredExactly(t *testing.T) {
	_, slave := pty(t)
	term := terminal.New(slave, slave)
	before := settings(t, slave)
	if before.Lflag&unix.ICANON == 0 || before.Lflag&unix.ECHO == 0 {
		t.Fatalf("a new terminal is not in line mode with echo: %#x", before.Lflag)
	}
	restore, err := term.Raw()
	if err != nil {
		t.Fatal(err)
	}
	during := settings(t, slave)
	if during.Lflag&(unix.ICANON|unix.ECHO) != 0 {
		t.Errorf("still in line mode or echoing: %#x", during.Lflag)
	}
	if during.Lflag&unix.ISIG == 0 || during.Oflag&unix.OPOST == 0 {
		t.Errorf("Ctrl-C or output processing was turned off: lflag %#x oflag %#x", during.Lflag, during.Oflag)
	}
	if during.Cc[unix.VSUSP] != 0 {
		t.Errorf("Ctrl-Z still stops the program in key mode: VSUSP = %#x", during.Cc[unix.VSUSP])
	}
	restore()
	restore()
	if after := settings(t, slave); after != before {
		t.Errorf("the terminal was not put back:\n before %+v\n after  %+v", before, after)
	}
}

func TestSizeIsTheTerminalsSize(t *testing.T) {
	master, slave := pty(t)
	if err := unix.IoctlSetWinsize(int(master.Fd()), unix.TIOCSWINSZ, &unix.Winsize{Row: 24, Col: 97}); err != nil {
		t.Fatal(err)
	}
	if w, h := terminal.New(slave, slave).Size(); w != 97 || h != 24 {
		t.Errorf("size = %dx%d, want 97x24", w, h)
	}
}

func TestRawOnSomethingThatIsNotATerminalFails(t *testing.T) {
	f, err := os.Open(os.DevNull)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if _, err := terminal.New(f, f).Raw(); err == nil {
		t.Error("Raw succeeded on a file that is not a terminal")
	}
	if w, h := terminal.New(f, f).Size(); w != 0 || h != 0 {
		t.Errorf("size = %dx%d, want zeros", w, h)
	}
}
