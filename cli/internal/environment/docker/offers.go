package docker

import (
	"context"
	"fmt"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// Offers probes the placement's machine and the resources its containers receive.
func (p *Provider) Offers(ctx context.Context) ([]*cliv1.AcceleratorOffer, error) {
	if p.cfg.NodeFactsPending {
		return nil, fmt.Errorf("compute node hardware has not been probed")
	}
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
	var offers []*cliv1.AcceleratorOffer
	for _, r := range p.cfg.Resources {
		got, err := r.Offers(ctx)
		if err != nil {
			return nil, err
		}
		offers = append(offers, got...)
	}
	return offers, nil
}
