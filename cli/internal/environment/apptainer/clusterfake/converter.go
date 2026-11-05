package clusterfake

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
)

// Converter is an apptainer.Converter kept in memory. Its SIF of an archive is SIF(archive). It is
// no more forgiving than the real one: an empty archive fails, and a Build that fails writes no
// SIF. It is safe for concurrent use.
type Converter struct {
	mu     sync.Mutex
	free   int64
	builds []string // the directory of each build that finished
	fail   func(op string) error
}

// NewConverter returns a converter with a petabyte free in every directory.
func NewConverter() *Converter { return &Converter{free: 1 << 50} }

// Version is what the fake says it is.
const Version = "apptainer version 1.4.5 (fake)"

// SIF is the fake's SIF of the archive.
func SIF(archive []byte) []byte {
	sum := sha256.Sum256(archive)
	return []byte("sif of " + hex.EncodeToString(sum[:]))
}

// SetFree sets the bytes free in every directory.
func (c *Converter) SetFree(free int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.free = free
}

// SetFaults makes the calls named by op ("free", "build", "describe") fail with what f returns.
func (c *Converter) SetFaults(f func(op string) error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.fail = f
}

// Builds returns the directory of every build that finished, in order.
func (c *Converter) Builds() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Clone(c.builds)
}

func (c *Converter) fault(ctx context.Context, op string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	c.mu.Lock()
	f := c.fail
	c.mu.Unlock()
	if f == nil {
		return nil
	}
	err := f(op)
	if errors.Is(err, ErrHang) {
		<-ctx.Done()
		return ctx.Err()
	}
	return err
}

// ErrHang, returned by a fault, makes the call hang until its context ends.
var ErrHang = errors.New("clusterfake: hang")

// Free implements apptainer.Converter.
func (c *Converter) Free(ctx context.Context, dir string) (int64, error) {
	if err := c.fault(ctx, "free"); err != nil {
		return 0, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.free, nil
}

// Build implements apptainer.Converter: it reads the whole archive, then writes its SIF.
func (c *Converter) Build(ctx context.Context, dir string, archive io.Reader, sif io.Writer) error {
	data, err := io.ReadAll(archive)
	if err != nil {
		return err
	}
	if err := c.fault(ctx, "build"); err != nil {
		return err
	}
	if len(data) == 0 {
		return &core.Failure{Kind: core.FailureUnavailable, Message: "clusterfake: FATAL: the archive is empty"}
	}
	if _, err := sif.Write(SIF(data)); err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.builds = append(c.builds, dir)
	return nil
}

// Describe implements apptainer.Converter.
func (c *Converter) Describe(ctx context.Context) (string, error) {
	if err := c.fault(ctx, "describe"); err != nil {
		return "", err
	}
	return Version, nil
}

var _ apptainer.Converter = (*Converter)(nil)
