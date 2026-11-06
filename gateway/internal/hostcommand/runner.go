// Package hostcommand owns bounded subprocess I/O. It knows no host methods or
// product contracts; callers supply the executable, arguments and stdin bytes.
package hostcommand

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"syscall"
	"time"
)

const MaxOutput = 1 << 20
const MaxStderr = 65536

// Command is one argv invocation.
type Command struct {
	Executable string
	Args       []string
	Input      []byte
}

// Result retains diagnostics even on cancellation, launch failure or nonzero exit.
type Result struct {
	Output   []byte
	Stderr   string
	ExitCode int
}

// Runner reaps its process before returning, honors context cancellation, bounds
// stdout and stderr, and never interprets the output. Errors retain Result.
type Runner interface {
	Run(context.Context, Command) (Result, error)
}

// ExecRunner runs an argv without a shell. Cancellation kills the command's
// process group and closes its pipes; WaitDelay also bounds inherited child pipes.
type ExecRunner struct{}

func (ExecRunner) Run(ctx context.Context, command Command) (Result, error) {
	if err := ctx.Err(); err != nil {
		return Result{ExitCode: -1}, err
	}
	cmd := exec.CommandContext(ctx, command.Executable, command.Args...)
	cmd.Stdin = bytes.NewReader(command.Input)
	// A wrapper's children must not outlive its timeout: they would keep running,
	// and keep the output pipes open until WaitDelay. So the group is killed.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
			return err
		}
		return nil
	}
	cmd.WaitDelay = time.Second
	output := &limitedBuffer{limit: MaxOutput}
	stderr := &limitedBuffer{limit: MaxStderr, truncate: true}
	cmd.Stdout, cmd.Stderr = output, stderr
	err := cmd.Run()
	result := Result{Output: output.buffer.Bytes(), Stderr: stderr.buffer.String(), ExitCode: -1}
	if cmd.ProcessState != nil {
		result.ExitCode = cmd.ProcessState.ExitCode()
	}
	if ctx.Err() != nil {
		err = ctx.Err()
	}
	return result, err
}

type limitedBuffer struct {
	buffer   bytes.Buffer
	limit    int
	truncate bool
}

func (b *limitedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	remaining := b.limit - b.buffer.Len()
	if n <= remaining {
		return b.buffer.Write(p)
	}
	_, _ = b.buffer.Write(p[:remaining])
	if b.truncate {
		return n, nil
	}
	return remaining, fmt.Errorf("host command stdout exceeds %d bytes: %w", b.limit, io.ErrShortBuffer)
}

// FakeRunner executes an in-memory program under the same output and context
// bounds as ExecRunner. Calls are synchronous; tests control cancellation in Program.
type FakeRunner struct {
	Program func(context.Context, Command) (Result, error)
}

func (r FakeRunner) Run(ctx context.Context, command Command) (Result, error) {
	if err := ctx.Err(); err != nil {
		return Result{ExitCode: -1}, err
	}
	result, err := r.Program(ctx, command)
	if len(result.Stderr) > MaxStderr {
		result.Stderr = result.Stderr[:MaxStderr]
	}
	if len(result.Output) > MaxOutput {
		result.Output = result.Output[:MaxOutput]
		err = errors.Join(err, io.ErrShortBuffer)
	}
	if ctx.Err() != nil {
		err = ctx.Err()
	}
	return result, err
}
