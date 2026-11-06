package dockerfake

import (
	"archive/tar"
	"bytes"
	"context"
	"fmt"
	"io"
	"strings"
	"sync/atomic"

	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// Source is a docker.Source made of a function.
type Source func(ctx context.Context, target environment.Target) (docker.Spec, error)

var _ docker.Source = Source(nil)

// Spec implements docker.Source.
func (s Source) Spec(ctx context.Context, target environment.Target) (docker.Spec, error) {
	return s(ctx, target)
}

// Fixed is a Source that gives every target the same spec.
func Fixed(spec docker.Spec) Source {
	return func(context.Context, environment.Target) (docker.Spec, error) { return spec, nil }
}

// IDs is a docker.IDs that counts. Two IDs with different prefixes never return the same id, as
// two processes' ids never do.
type IDs struct {
	prefix uint32
	n      atomic.Uint64
}

var _ docker.IDs = (*IDs)(nil)

// NewIDs returns ids made of the process number and a count.
func NewIDs(process uint32) *IDs { return &IDs{prefix: process} }

// New implements docker.IDs.
func (i *IDs) New() string { return fmt.Sprintf("%08x%08x", i.prefix, i.n.Add(1)) }

// fromRefs returns the reference of each plain "FROM <ref>" line of the Dockerfile at the root of
// a build context. The fake reads only that form, apart from the environment's own reading of a
// Dockerfile, so a test of what the environment reads does not test it against itself.
func fromRefs(buildContext []byte) []string {
	r := tar.NewReader(bytes.NewReader(buildContext))
	for {
		h, err := r.Next()
		if err != nil {
			return nil
		}
		if h.Name != "Dockerfile" {
			continue
		}
		text, err := io.ReadAll(r)
		if err != nil {
			return nil
		}
		var refs []string
		for _, line := range strings.Split(string(text), "\n") {
			if f := strings.Fields(line); len(f) >= 2 && strings.EqualFold(f[0], "FROM") {
				refs = append(refs, f[1])
			}
		}
		return refs
	}
}
