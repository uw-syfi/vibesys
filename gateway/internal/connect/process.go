package connect

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

const (
	// ReportFD is the descriptor a candidate reports on: the first after stdin, stdout and stderr.
	ReportFD = 3
	// stderrTail bounds what a failed start reports of what the candidate wrote.
	stderrTail = 4096
	// logRotation is how long a host's log may grow before a start sets it aside.
	logRotation = 1 << 20
	// endGrace is how long a candidate that lost its starter has to exit before it is killed.
	endGrace = 5 * time.Second
)

// Process is the Launcher of a real host: Binary with Args, detached in a session of its own, in Dir
// (the root) and with exactly the environment Env.
type Process struct {
	Binary string
	Args   []string
	Env    []string
	Dir    string
	// Log is the file the candidate's stderr is appended to.
	Log string
}

// Launch implements Launcher.
func (p Process) Launch(ctx context.Context) (*pb.HostStartReport, error) {
	if err := os.MkdirAll(p.Dir, 0o700); err != nil {
		return nil, err
	}
	from := setAside(p.Log)
	reader, writer, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	defer reader.Close()
	command, err := p.start(writer)
	writer.Close()
	if err != nil {
		return nil, err
	}
	ended := make(chan error, 1)
	go func() { ended <- command.Wait() }()
	said := make(chan []byte, 1)
	go func() {
		data, _ := io.ReadAll(reader)
		said <- data
	}()
	var data []byte
	select {
	case data = <-said:
	case <-ctx.Done():
		end(command, ended)
		return nil, fmt.Errorf("host startup: %w", ctx.Err())
	}
	if len(data) == 0 {
		// The pipe closes when the candidate is gone, so it has ended or is about to.
		return nil, p.silent(ctx, command, ended, from)
	}
	var report pb.HostStartReport
	if err := proto.Unmarshal(data, &report); err != nil {
		end(command, ended)
		return nil, fmt.Errorf("the host's start report: %w", err)
	}
	if err := protovalidate.Validate(&report); err != nil {
		end(command, ended)
		return nil, fmt.Errorf("the host's start report: %w", err)
	}
	if report.GetServing() != nil {
		return &report, nil
	}
	// A candidate that will not serve reports and exits; wait for that, so that a refusal is only
	// returned once nothing of the attempt is left running.
	select {
	case <-ended:
	case <-ctx.Done():
		end(command, ended)
		return nil, fmt.Errorf("host startup: %w", ctx.Err())
	}
	return &report, nil
}

func (p Process) start(report *os.File) (*exec.Cmd, error) {
	null, err := os.OpenFile(os.DevNull, os.O_RDWR, 0)
	if err != nil {
		return nil, err
	}
	defer null.Close()
	stderr, err := os.OpenFile(p.Log, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	defer stderr.Close()
	command := exec.Command(p.Binary, p.Args...)
	// A host inherits neither SSH's pipes nor its controlling session.
	command.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	command.Env = p.Env
	command.Dir = p.Dir
	command.Stdin, command.Stdout, command.Stderr = null, null, stderr
	command.ExtraFiles = []*os.File{report}
	return command, command.Start()
}

// silent is the failure of a candidate that said nothing: how it ended and what this attempt wrote.
func (p Process) silent(ctx context.Context, command *exec.Cmd, ended <-chan error, from int64) error {
	select {
	case err := <-ended:
		status := "exit 0"
		if err != nil {
			status = err.Error()
		}
		return &EndedError{Status: status, Stderr: writtenSince(p.Log, from)}
	case <-ctx.Done():
		end(command, ended)
		return fmt.Errorf("host startup: %w", ctx.Err())
	}
}

// end ends a candidate nobody is waiting for: asked first, then killed. A host in an unknown state
// must not outlive the start that gave up on it.
func end(command *exec.Cmd, ended <-chan error) {
	_ = command.Process.Signal(syscall.SIGTERM)
	select {
	case <-ended:
	case <-time.After(endGrace):
		_ = command.Process.Kill()
		<-ended
	}
}

// setAside keeps the log from growing for ever, and says where this attempt's output will begin.
func setAside(log string) int64 {
	info, err := os.Stat(log)
	if err != nil {
		return 0
	}
	if info.Size() < logRotation || os.Rename(log, log+".old") != nil {
		return info.Size()
	}
	return 0
}

// writtenSince is what was appended to log after offset from, at most its newest stderrTail bytes.
func writtenSince(log string, from int64) string {
	file, err := os.Open(log)
	if err != nil {
		return ""
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return ""
	}
	from = max(from, info.Size()-stderrTail)
	if from >= info.Size() {
		return ""
	}
	tail := make([]byte, info.Size()-from)
	if _, err := file.ReadAt(tail, from); err != nil && !errors.Is(err, io.EOF) {
		return ""
	}
	return strings.TrimSpace(string(tail))
}
