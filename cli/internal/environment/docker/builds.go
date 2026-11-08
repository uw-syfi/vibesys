package docker

import (
	"context"
	"fmt"
	"io"
	"sync"
)

// Builds is the lock of the builds of each tag on one engine. The providers of the placements
// whose engine is one Docker (several placements of one machine, or the apptainer placements that
// all build with this machine's Docker) must share one, so that two jobs that need one image build
// it once: the second waits for the first and uses its image. Without it both build, and the one
// that ends second fails as BuildKit refuses to name an image whose tag the first one took ("image
// ... already exists"). Create it with NewBuilds. It is safe for concurrent use.
type Builds struct {
	mu   sync.Mutex
	held map[string]chan struct{} // by tag: closed when the build of the tag ends
}

// NewBuilds returns a lock that holds no build.
func NewBuilds() *Builds { return &Builds{held: map[string]chan struct{}{}} }

// lock takes the lock of the tag's build, waiting while another holds it, and returns its release.
// It says in log, once, that it waits.
func (b *Builds) lock(ctx context.Context, tag, prefix string, log io.Writer) (func(), error) {
	said := false
	for {
		b.mu.Lock()
		ended, busy := b.held[tag]
		if !busy {
			mine := make(chan struct{})
			b.held[tag] = mine
			b.mu.Unlock()
			return func() {
				b.mu.Lock()
				delete(b.held, tag)
				b.mu.Unlock()
				close(mine)
			}, nil
		}
		b.mu.Unlock()
		if !said {
			said = true
			_, _ = fmt.Fprintf(log, "%s another job is building %s; waiting for it to end, to use its image\n", prefix, tag)
		}
		select {
		case <-ended:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
}
