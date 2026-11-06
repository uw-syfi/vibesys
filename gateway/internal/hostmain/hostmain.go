// Package hostmain composes the host services and owns process startup.
package hostmain

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/buildinfo"
	"github.com/uw-syfi/vibesys/gateway/internal/connect"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/gateway/internal/pipe"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	schema "github.com/uw-syfi/vibesys/gateway/schemahash"
	"github.com/uw-syfi/vibesys/orchestrator/hosted"
	"github.com/uw-syfi/vibesys/orchestrator/hostlock"
)

// Main runs the host command and returns its process exit status.
func Main(args []string) int {
	// A closed SSH/stdout pipe is a normal disconnect. Let Copy observe EPIPE
	// and settle both workers instead of the runtime terminating on fd 1.
	signal.Ignore(syscall.SIGPIPE)
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx, args); err != nil {
		var code exitCode
		if errors.As(err, &code) {
			return int(code)
		}
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

func run(ctx context.Context, args []string) error {
	if len(args) == 1 && args[0] == "--version" {
		_, err := fmt.Fprintf(os.Stdout, "%s %s\n", hosted.HostProgram, buildinfo.Version)
		return err
	}
	if len(args) > 0 && hosted.IsCommand(args[0]) {
		if code := hosted.Command(ctx, buildinfo.Version, args, os.Stdin, os.Stdout, os.Stderr); code != 0 {
			return exitCode(code)
		}
		return nil
	}
	if len(args) == 0 || (args[0] != "serve" && args[0] != "connect") {
		return errors.New("usage: " + hosted.HostProgram + " {serve|connect} [options]\n  " + hosted.HostProgram + " VERB, where VERB is one of\n" + hosted.Usage)
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
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected argument %q", flags.Arg(0))
	}
	if timeout <= 0 {
		return errors.New("timeout must be positive")
	}
	directory, err := gatewayRoot(*root)
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
	record, err := hostlock.Marshal(os.Getpid(), buildinfo.Version)
	if err != nil {
		return err
	}
	if err := endpoint.Record(record); err != nil {
		return err
	}
	monitor := lifecycle.NewMonitor(idle, lifecycle.SystemClock{}, func() { endpoint.Listener.Close() })
	defer monitor.Stop()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	orchestrator, err := startOrchestrator(ctx, cancel)
	if err != nil {
		return err
	}
	go holdWhileLive(ctx, orchestrator.Live(), monitor)
	served := make(chan error, 1)
	go func() {
		served <- server.Serve(ctx, lifecycle.Track(endpoint.Listener, monitor), server.Config{Root: root, Version: buildinfo.Version, Register: orchestrator.Register})
	}()
	select {
	case err := <-served:
		cancel()
		<-orchestrator.Done()
		return err
	case err, ok := <-orchestrator.Done():
		if !ok || err == nil {
			// No orchestrator in this process, or a clean stop: HostService serves on.
			err = <-served
			return err
		}
		cancel()
		<-served
		return fmt.Errorf("the orchestrator stopped: %w", err)
	}
}

func bridge(ctx context.Context, root string, startup time.Duration) error {
	binary, err := os.Executable()
	if err != nil {
		return err
	}
	startupCtx, cancel := context.WithTimeout(ctx, startup)
	connection, err := connect.Dial(startupCtx, lifecycle.SocketPath(root, schema.APISchemaHash), binary, []string{"serve", "--root", root}, os.Stderr)
	cancel()
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
