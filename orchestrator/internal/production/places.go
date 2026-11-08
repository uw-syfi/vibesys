package production

import (
	"context"
	"errors"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// CLIPlaces is the host's eval places as the job tool's daemon reports them: each call asks the tool
// where its daemon is, so a repaired or replaced install is used by the next call, then asks the daemon.
type CLIPlaces struct {
	// JobTool is the endpoint of the vibesys-cli the host's runs use.
	JobTool func(context.Context) (core.JobToolEndpoint, error)
	// Runner runs the tool.
	Runner Runner
	// Connect dials the socket the tool answered.
	Connect clidaemon.PlacementConnector
}

// NewCLIPlaces is the source of a real host.
func NewCLIPlaces(jobTool func(context.Context) (core.JobToolEndpoint, error)) CLIPlaces {
	return CLIPlaces{JobTool: jobTool, Runner: ExecRunner{}, Connect: connectCLIPlacements}
}

// Places implements daemon.PlaceSource.
func (p CLIPlaces) Places(ctx context.Context, refresh bool) ([]*pb.EvalPlace, error) {
	if p.JobTool == nil {
		return nil, errors.New("this host has no job tool, so no place to evaluate on")
	}
	endpoint, err := p.JobTool(ctx)
	if err != nil {
		return nil, err
	}
	return clidaemon.Places{Ensurer: ExecEnsurerFor(endpoint, p.Runner), Connect: p.Connect}.List(ctx, refresh)
}

func connectCLIPlacements(_ context.Context, socket clidaemon.Socket) (clidaemon.PlacementConnection, error) {
	return clidaemon.NewClient(socket, CLIUnixDialer{})
}
