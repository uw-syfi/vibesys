package hostcommand_test

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
)

func TestProgram(t *testing.T) {
	args := os.Args
	for i, arg := range args {
		if arg != "--host-command-program" {
			continue
		}
		mode := args[i+1]
		switch mode {
		case "echo":
			data, _ := io.ReadAll(os.Stdin)
			fmt.Fprint(os.Stdout, string(data)+os.Getenv("VIBESYS_STATE_HOME"))
		case "fail":
			fmt.Fprint(os.Stderr, "invalid configuration")
			os.Exit(7)
		case "large":
			fmt.Fprint(os.Stdout, strings.Repeat("x", hostcommand.MaxOutput+1))
		case "stderr":
			fmt.Fprint(os.Stderr, strings.Repeat("x", hostcommand.MaxStderr+10))
		}
		os.Exit(0)
	}
}

// Every implementation runs the same contract, including failures and limits.
func TestRunnerContract(t *testing.T) {
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, implementation := range []string{"exec", "fake"} {
		t.Run(implementation, func(t *testing.T) {
			var runner hostcommand.Runner = hostcommand.ExecRunner{}
			if implementation == "fake" {
				runner = hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
					if c.Executable == "/missing/vibesys" {
						return hostcommand.Result{ExitCode: -1}, os.ErrNotExist
					}
					switch c.Args[len(c.Args)-1] {
					case "echo":
						return hostcommand.Result{Output: append(append([]byte{}, c.Input...), []byte(c.StateRoot)...)}, nil
					case "fail":
						return hostcommand.Result{ExitCode: 7, Stderr: "invalid configuration"}, errors.New("exit status 7")
					case "large":
						return hostcommand.Result{Output: []byte(strings.Repeat("x", hostcommand.MaxOutput+1))}, nil
					case "stderr":
						return hostcommand.Result{Stderr: strings.Repeat("x", hostcommand.MaxStderr+10)}, nil
					default:
						panic("unknown program")
					}
				}}
			}
			run := func(ctx context.Context, mode string) (hostcommand.Result, error) {
				return runner.Run(ctx, hostcommand.Command{Executable: binary, Args: []string{"-test.run=^TestProgram$", "--", "--host-command-program", mode}, Input: []byte{0, 255, 10}, StateRoot: "/state"})
			}
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			result, err := run(ctx, "echo")
			if err != nil || string(result.Output) != string([]byte{0, 255, 10})+"/state" || result.ExitCode != 0 {
				t.Fatalf("echo: %+v %v", result, err)
			}
			result, err = run(ctx, "fail")
			if err == nil || result.ExitCode != 7 || result.Stderr != "invalid configuration" {
				t.Fatalf("failure: %+v %v", result, err)
			}
			result, err = run(ctx, "large")
			if err == nil || len(result.Output) > hostcommand.MaxOutput {
				t.Fatalf("stdout limit: %d %v", len(result.Output), err)
			}
			result, err = run(ctx, "stderr")
			if err != nil || len(result.Stderr) != hostcommand.MaxStderr {
				t.Fatalf("stderr limit: %d %v", len(result.Stderr), err)
			}
			_, err = runner.Run(ctx, hostcommand.Command{Executable: "/missing/vibesys"})
			if err == nil {
				t.Fatal("missing executable succeeded")
			}
			cancelled, stop := context.WithCancel(ctx)
			stop()
			_, err = run(cancelled, "echo")
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("cancelled: %v", err)
			}
			expired, finish := context.WithDeadline(ctx, time.Time{})
			defer finish()
			_, err = run(expired, "echo")
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatalf("deadline: %v", err)
			}
		})
	}
}
