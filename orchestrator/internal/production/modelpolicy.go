package production

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// catalogBound limits the one call that asks the sidecar program which models each harness runs.
const catalogBound = 60 * time.Second

// ModelCatalog says which models each harness runs, its default first. The sidecar program knows,
// through agentshim, so it is the one asked: this host keeps no list of model names.
type ModelCatalog interface {
	Models(ctx context.Context) (map[core.Harness][]core.ModelName, error)
}

// SidecarCatalog asks the program that holds a session, with its `catalog` argument. An answer is kept
// for the life of the host; an error is not, so the next ask tries again.
type SidecarCatalog struct {
	Parts  Parts
	Runner Runner
	// Environ is the environment the program runs in: the host's own, as hostconfig.Environ makes it.
	Environ []string

	mu    sync.Mutex
	known map[core.Harness][]core.ModelName
}

// Models implements ModelCatalog.
func (c *SidecarCatalog) Models(ctx context.Context) (map[core.Harness][]core.ModelName, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.known != nil {
		return c.known, nil
	}
	command, _, err := c.Parts.SessionHolder()
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, catalogBound)
	defer cancel()
	stdout, stderr, err := c.Runner.Run(ctx, command[0], append(append([]string{}, command[1:]...), "catalog"), c.Environ)
	if err != nil {
		return nil, fmt.Errorf("ask %s for its models: %w: %s", command[0], err, failureOf(stderr))
	}
	var catalog pb.ModelCatalog
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(stdout, &catalog); err != nil {
		return nil, fmt.Errorf("%s did not answer which models it runs: %w", command[0], err)
	}
	known := map[core.Harness][]core.ModelName{}
	for _, entry := range wire.ModelCatalogIn(&catalog) {
		known[entry.Harness] = entry.Models
	}
	c.known = known
	return known, nil
}

// HostModels is the host's model policy: the allowed_models of its settings file, read afresh each
// time it is asked, and what each harness runs, from its catalog. It implements daemon.ModelPolicySource.
//
// A settings file that cannot be read gives a closed policy, which allows nothing, and a catalog that
// cannot be read leaves Known empty, so an agent that names no model on a host that restricts models is
// refused: neither failure ever reads as no policy.
type HostModels struct {
	// Home is the home directory the settings file is in.
	Home    string
	Catalog ModelCatalog
	Log     *slog.Logger
}

// ModelPolicy implements daemon.ModelPolicySource.
func (h HostModels) ModelPolicy() core.ModelPolicy {
	var policy core.ModelPolicy
	allowed, err := hostconfig.LoadAllowedModels(h.Home)
	if err != nil {
		h.log().Error("the host's model policy cannot be read, so no model is allowed", "err", err)
		policy.Closed = true
	}
	for _, m := range allowed {
		policy.Allowed = append(policy.Allowed, core.ModelName(m))
	}
	ctx, cancel := context.WithTimeout(context.Background(), catalogBound)
	defer cancel()
	known, err := h.Catalog.Models(ctx)
	if err != nil {
		h.log().Warn("the models each harness runs are not known", "err", err)
	}
	policy.Known = known
	return policy
}

func (h HostModels) log() *slog.Logger {
	if h.Log == nil {
		return slog.New(slog.DiscardHandler)
	}
	return h.Log
}
