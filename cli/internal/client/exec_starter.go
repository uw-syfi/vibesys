package client

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"syscall"

	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// ExecStarter starts the daemon as a process of its own: it runs Command, which must run the
// daemon (daemon.Run) with the readiness pipe on file descriptor runtimedir.ReadyFD, in a session of
// its own so that it outlives the terminal and the client that started it.
type ExecStarter struct {
	Dir     runtimedir.Dir
	Command func() *exec.Cmd
}

var _ Starter = ExecStarter{}

// logTail is how much of the daemon's log a failed start reports.
const logTail = 2048

// Start implements Starter. It returns when the daemon says it serves; a daemon that exits first
// closes the pipe, and the start fails with the end of its log, not after a wait of some length.
func (s ExecStarter) Start(ctx context.Context) error {
	r, w, err := os.Pipe()
	if err != nil {
		return err
	}
	defer r.Close()
	logFile, err := os.OpenFile(s.Dir.Log(), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		w.Close()
		return fmt.Errorf("opening the daemon log: %w", err)
	}
	defer logFile.Close()

	cmd := s.Command()
	cmd.Stdin, cmd.Stdout, cmd.Stderr = nil, logFile, logFile
	cmd.ExtraFiles = []*os.File{w}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		w.Close()
		return fmt.Errorf("starting the daemon: %w", err)
	}
	// Only the daemon holds the write end now, so its exit closes the pipe.
	w.Close()

	stop := context.AfterFunc(ctx, func() { r.Close() })
	defer stop()
	buf := make([]byte, 1)
	if _, err := io.ReadFull(r, buf); err != nil {
		cmd.Process.Kill()
		cmd.Wait()
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return wire.Unavailable(fmt.Sprintf(
			"the daemon did not start; the end of %s says:\n%s", s.Dir.Log(), tailOf(s.Dir.Log())))
	}
	if buf[0] != runtimedir.ReadyByte {
		cmd.Process.Kill()
		cmd.Wait()
		return fmt.Errorf("the daemon answered %q on its readiness pipe", buf)
	}
	// The daemon is not ours to wait for: it lives on after this process.
	return cmd.Process.Release()
}

func tailOf(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return "(unreadable: " + err.Error() + ")"
	}
	if len(data) > logTail {
		data = data[len(data)-logTail:]
	}
	return string(data)
}
