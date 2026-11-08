// Package hostmain composes the host services and owns process startup.
package hostmain

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/buildinfo"
	"github.com/uw-syfi/vibesys/gateway/internal/connect"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/gateway/internal/pipe"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	schema "github.com/uw-syfi/vibesys/gateway/schemahash"
	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
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

// serveOptions are the flags of serve.
type serveOptions struct {
	idle time.Duration
	// orchestratorOptional keeps a host that cannot serve the orchestrator up, serving the host API.
	// The desktop app's bridge asks for it; without it such a host refuses to start.
	orchestratorOptional bool
	// reportFD is the descriptor to say how the start went on, or 0 for nobody listening.
	reportFD int
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
	root := flags.String("root", "", "the host's root ("+gatewayroot.Rule+")")
	options := serveOptions{idle: 10 * time.Minute}
	timeout := 10 * time.Second
	if command == "serve" {
		flags.DurationVar(&options.idle, "idle-timeout", options.idle, "exit after this period without connections")
		flags.BoolVar(&options.orchestratorOptional, optionalOrchestratorFlag, false, "serve the host API even when the orchestrator cannot be served")
		flags.IntVar(&options.reportFD, reportFDFlag, 0, "the descriptor to report the start on")
	} else {
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
	if timeout <= 0 || options.idle <= 0 {
		return errors.New("timeout must be positive")
	}
	directory, err := gatewayRoot(*root)
	if err != nil {
		return err
	}
	if command == "serve" {
		return serve(ctx, directory, options)
	}
	return bridge(ctx, directory, timeout)
}

const (
	optionalOrchestratorFlag = "orchestrator-optional"
	reportFDFlag             = "start-report-fd"
)

func serve(ctx context.Context, root string, options serveOptions) error {
	report := connect.OpenReport(options.reportFD)
	defer report.Close()
	held, err := lifecycle.Hold(root)
	if errors.Is(err, lifecycle.ErrServing) {
		report.AlreadyServed()
		return nil
	}
	if err != nil {
		return err
	}
	var endpoint *lifecycle.Endpoint
	defer func() {
		if endpoint != nil {
			endpoint.Close()
		} else {
			held.Close()
		}
	}()
	record, err := hostlock.Marshal(os.Getpid(), buildinfo.Version, root, hostlock.ThisMachine())
	if err != nil {
		return err
	}
	if err := held.Record(record); err != nil {
		return err
	}
	config, err := hostconfig.Load(root)
	if err != nil {
		return report.RefuseConfig(err)
	}
	getenv, home, err := becomeHost(root, config)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var connections lifecycle.Connections
	orchestrator, err := startOrchestrator(ctx, cancel, root, config, getenv, home, connections.Open)
	if err != nil {
		return err
	}
	if why := orchestrator.Unserved(); why != nil && !options.orchestratorOptional {
		return report.Refuse(why)
	}
	endpoint, err = held.Publish()
	if err != nil {
		cancel()
		<-orchestrator.Done()
		return err
	}
	monitor := lifecycle.NewMonitor(options.idle, lifecycle.SystemClock{}, func() { endpoint.Listener.Close() })
	defer monitor.Stop()
	go holdWhileLive(ctx, orchestrator.Live(), monitor)
	served := make(chan error, 1)
	go func() {
		served <- server.Serve(ctx, lifecycle.Track(endpoint.Listener, monitor, &connections), server.Config{Root: root, Version: buildinfo.Version, Register: orchestrator.Register})
	}()
	report.Serving()
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

// becomeHost makes this process's environment and working directory the host's, whatever it
// inherited, so that neither this process nor anything it starts sees a setting of whoever started
// it. It returns how to read what is left, and the home directory.
func becomeHost(root string, config *hostconfig.Config) (func(string) string, string, error) {
	home, _ := os.UserHomeDir()
	environment := hostconfig.Environ(config, home, os.Getenv)
	os.Clearenv()
	for _, entry := range environment {
		name, value, _ := strings.Cut(entry, "=")
		if err := os.Setenv(name, value); err != nil {
			return nil, "", err
		}
	}
	if err := os.Chdir(root); err != nil {
		return nil, "", err
	}
	return os.Getenv, home, nil
}

func bridge(ctx context.Context, root string, startup time.Duration) error {
	binary, err := os.Executable()
	if err != nil {
		return err
	}
	home, _ := os.UserHomeDir()
	launcher := connect.Process{
		Binary: binary, Args: []string{"serve", "--root", root, "--" + optionalOrchestratorFlag, "--" + reportFDFlag, strconv.Itoa(connect.ReportFD)},
		Env: hostconfig.Environ(nil, home, os.Getenv), Dir: root, Log: lifecycle.LogPath(root),
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	startupCtx, cancel := context.WithTimeout(ctx, startup)
	connection, err := connect.DialForAPI(startupCtx, lifecycle.SocketPath(root), launcher, connect.HoldingOf(root), buildinfo.Version, schema.APISchemaHash, func(id *hostconfig.IdentifyResponse) {
		message, attrs := stayedLog(id)
		log.Warn(message, attrs...)
	})
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

// stayedLog explains why a running host of another API kept the socket, for the
// operator log, and names the decisive reason instead of always claiming live
// runs. DialForAPI keeps such a host when it is a newer release, when it has
// live runs, or when it has other clients connected; a host that declined to
// stop after being asked raced to stay busy. The relation is checked first,
// mirroring DialForAPI, so a newer host reports as newer even when it also has
// runs or clients.
func stayedLog(id *hostconfig.IdentifyResponse) (string, []any) {
	switch {
	case id.GetRelation() == hostconfig.ReleaseRelationHostNewer:
		return "the running host speaks a newer release, so it stays", []any{"release", id.GetRelease()}
	case id.GetLiveRuns() > 0:
		return "the running host speaks another API and has live runs, so it stays", []any{"live_runs", id.GetLiveRuns(), "release", id.GetRelease()}
	case id.GetConnectedClients() > 0:
		return "the running host speaks another API and has other connected clients, so it stays", []any{"connected_clients", id.GetConnectedClients(), "release", id.GetRelease()}
	default:
		return "the running host speaks another API and declined to stop, so it stays", []any{"release", id.GetRelease()}
	}
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
