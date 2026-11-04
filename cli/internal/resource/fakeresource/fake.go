// Package fakeresource is the in-memory Fake of resource.Resource, for the tests of what applies
// resources, such as the docker environment. It passes the contract in resourcetest, so it is no
// more forgiving than a real resource: a context that is done stops it, and a Fake that is broken
// fails its fingerprint as unavailable.
package fakeresource

import (
	"context"
	"maps"
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// Fake is a resource of any kind that reports the facts and attaches the attachment it is given.
type Fake struct {
	Of       core.ResourceKind
	Facts    map[string]string
	Attached resource.Attachment
	Claimed  []core.Claim

	mu     sync.Mutex
	broken bool
}

var _ resource.Resource = (*Fake)(nil)

// Break makes every later Fingerprint fail as unavailable.
func (f *Fake) Break() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.broken = true
}

// Kind implements resource.Resource.
func (f *Fake) Kind() core.ResourceKind { return f.Of }

// Claims implements resource.Resource.
func (f *Fake) Claims() []core.Claim { return slices.Clone(f.Claimed) }

// Fingerprint implements resource.Resource.
func (f *Fake) Fingerprint(ctx context.Context) (map[string]string, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.broken {
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: "fakeresource: broken"}
	}
	return maps.Clone(f.Facts), nil
}

// Attach implements resource.Resource. What it attaches is validated as a real resource's is.
func (f *Fake) Attach(ctx context.Context) (resource.Attachment, error) {
	if err := ctx.Err(); err != nil {
		return resource.Attachment{}, err
	}
	if err := f.Attached.Validate(); err != nil {
		return resource.Attachment{}, err
	}
	a := f.Attached
	a.Mounts = slices.Clone(a.Mounts)
	a.Env = maps.Clone(a.Env)
	return a, nil
}
