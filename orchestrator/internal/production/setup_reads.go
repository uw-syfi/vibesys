package production

import (
	"context"
	"fmt"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// CLISetupReads reads a run's setup facts from the job tool's daemon. Each read asks the tool where
// its daemon is, so a repaired or replaced install is used by the next read, then asks the daemon,
// in a goroutine of its own, so that the daemon's loop never waits on it.
type CLISetupReads struct {
	// Runner runs the tool.
	Runner Runner
	// Connect dials the socket the tool answered.
	Connect clidaemon.SetupConnector
}

// NewCLISetupReads is the reader of a real host.
func NewCLISetupReads() CLISetupReads {
	return CLISetupReads{Runner: ExecRunner{}, Connect: connectCLISetup}
}

var _ daemon.SetupReads = CLISetupReads{}

// Start implements daemon.SetupReads. The context is the bound of the whole read, the tool's start
// included, and a read that outlives it fails.
func (c CLISetupReads) Start(ctx context.Context, ref daemon.SetupRef, done func(setup.Read)) {
	go func() { done(c.read(ctx, ref)) }()
}

// read makes one read and says what it found. A daemon that does not answer, answers that it cannot
// describe a setup, or answers facts that do not meet the contract or cannot be mapped is a failed
// read, whose facts nobody keeps: it is never a pass.
func (c CLISetupReads) read(ctx context.Context, ref daemon.SetupRef) setup.Read {
	answer, err := clidaemon.SetupReader{Ensurer: ExecEnsurerFor(ref.JobTool, c.Runner), Connect: c.Connect}.Read(ctx, ref.Repository, ref.Task)
	if err != nil {
		return setup.Read{Kind: setup.ReadFailure, Detail: err.Error()}
	}
	if answer.Unreadable != "" {
		return setup.Read{Kind: setup.ReadUnreadable, Detail: answer.Unreadable}
	}
	facts, err := wire.SetupFactsIn(answer.Facts)
	if err != nil {
		return setup.Read{Kind: setup.ReadFailure, Detail: fmt.Sprintf("the CLI daemon's setup facts cannot be read: %v", err)}
	}
	return setup.Read{Kind: setup.ReadFacts, Facts: facts}
}

func connectCLISetup(_ context.Context, socket clidaemon.Socket) (clidaemon.SetupConnection, error) {
	return clidaemon.NewClient(socket, CLIUnixDialer{})
}
