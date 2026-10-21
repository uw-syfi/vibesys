// vibesysd serves the host API, or connects stdio to a detached serving process.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	schema "github.com/uw-syfi/vibesys/gateway/gen"
	"github.com/uw-syfi/vibesys/gateway/internal/connect"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/gateway/internal/pipe"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
)

const version = "0.1.0"

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx, os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string) error {
	if len(args) == 0 || (args[0] != "serve" && args[0] != "connect") {
		return errors.New("usage: vibesysd {serve|connect} [options]")
	}
	command := args[0]
	flags := flag.NewFlagSet(command, flag.ContinueOnError)
	root := flags.String("root", "", "host .vibesys directory (default: ~/.vibesys)")
	timeout := 10 * time.Minute
	if command == "serve" {
		flags.DurationVar(&timeout, "idle-timeout", timeout, "exit after this period without connections")
	} else {
		timeout = 10 * time.Second
		flags.DurationVar(&timeout, "startup-timeout", timeout, "maximum wait for a serving gateway")
	}
	if err := flags.Parse(args[1:]); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected argument %q", flags.Arg(0))
	}
	if timeout <= 0 {
		return errors.New("timeout must be positive")
	}
	if *root == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return err
		}
		*root = filepath.Join(home, ".vibesys")
	}
	directory, err := filepath.Abs(*root)
	if err != nil {
		return err
	}
	if command == "serve" {
		return serve(ctx, directory, timeout)
	}
	return bridge(ctx, directory, timeout)
}

func serve(ctx context.Context, root string, idle time.Duration) error {
	endpoint, err := lifecycle.Listen(root, schema.APISchemaHash)
	if errors.Is(err, lifecycle.ErrServing) {
		return nil
	}
	if err != nil {
		return err
	}
	defer endpoint.Close()
	monitor := lifecycle.NewMonitor(idle, lifecycle.SystemClock{}, func() { endpoint.Listener.Close() })
	defer monitor.Stop()
	return server.Serve(ctx, lifecycle.Track(endpoint.Listener, monitor), server.Config{Root: root, Version: version})
}

func bridge(ctx context.Context, root string, startup time.Duration) error {
	binary, err := os.Executable()
	if err != nil {
		return err
	}
	startupCtx, cancel := context.WithTimeout(ctx, startup)
	defer cancel()
	connection, err := connect.Dial(startupCtx, lifecycle.SocketPath(root, schema.APISchemaHash), binary, []string{"serve", "--root", root})
	if err != nil {
		return err
	}
	streams, err := standardStreams()
	if err != nil {
		connection.Close()
		return err
	}
	return pipe.Copy(ctx, streams, connection)
}

type streams struct{ input, output *os.File }

func (s streams) Read(p []byte) (int, error)  { return s.input.Read(p) }
func (s streams) Write(p []byte) (int, error) { return s.output.Write(p) }
func (s streams) Close() error                { return errors.Join(s.input.Close(), s.output.Close()) }

// Register nonblocking descriptors with Go's poller so Close interrupts both
// copy workers, including a client that never supplies another byte on stdin.
func standardStreams() (io.ReadWriteCloser, error) {
	for _, descriptor := range []int{0, 1} {
		if err := syscall.SetNonblock(descriptor, true); err != nil {
			return nil, err
		}
	}
	return streams{input: os.NewFile(0, "stdin"), output: os.NewFile(1, "stdout")}, nil
}
