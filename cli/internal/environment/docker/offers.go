package docker

import (
	"context"
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Offers probes the placement's machine and the resources its containers receive.
func (p *Provider) Offers(ctx context.Context) ([]core.Accelerator, error) {
	if _, err := p.cfg.Engine.Containers(ctx, Labels{}); err != nil {
		return nil, err
	}
	machine, err := p.cfg.Backend.Machine(ctx)
	if err != nil {
		return nil, err
	}
	if machine.HostID == "" {
		return nil, fmt.Errorf("the compute node has not been observed")
	}
	var offers []core.Accelerator
	for _, r := range p.cfg.Resources {
		got, err := r.Offers(ctx)
		if err != nil {
			return nil, err
		}
		offers = append(offers, got...)
	}
	return offers, nil
}
