// Package upgradetest is the in-memory release that upgrade's tests, and the tests of the
// command that calls it, run against. It satisfies upgrade.Source directly and also as an
// http.RoundTripper, so the HTTP implementation and the Fake pass one contract suite.
package upgradetest

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/upgrade"
)

// Release is a published set of assets plus the faults a test applies to reads of them. It is
// safe for concurrent use.
type Release struct {
	mu     sync.Mutex
	assets map[string][]byte
	fault  map[string]Fault
	opened []string
}

// Fault changes what a read of one asset returns, so a test can reproduce a broken download.
type Fault struct {
	// Truncate, when set, serves only the first TruncateAt bytes and then reports a clean end of
	// file, as a proxy that cuts a body short does.
	Truncate   bool
	TruncateAt int
	// Fail, when set, serves the first FailAfter bytes and then fails the read, as a dropped
	// connection does.
	Fail      bool
	FailAfter int
}

// ErrInjected is the read failure a Fault with Fail set produces.
var ErrInjected = errors.New("injected connection failure")

// New returns a complete release of the given version. binaries maps a platform to the bytes of
// its binary; checksums.txt and VERSION are derived from them.
func New(version string, binaries map[upgrade.Platform][]byte) *Release {
	r := &Release{assets: map[string][]byte{}, fault: map[string]Fault{}}
	r.assets[upgrade.VersionAsset] = []byte(version + "\n")
	var lines []string
	for platform, content := range binaries {
		name, err := upgrade.AssetName(platform)
		if err != nil {
			panic(err)
		}
		r.assets[name] = content
		sum := sha256.Sum256(content)
		lines = append(lines, hex.EncodeToString(sum[:])+"  "+name)
	}
	sort.Strings(lines)
	r.assets[upgrade.ChecksumsAsset] = []byte(strings.Join(lines, "\n") + "\n")
	return r
}

// Set replaces or adds an asset's content, leaving the others as they are.
func (r *Release) Set(asset string, content []byte) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.assets[asset] = bytes.Clone(content)
}

// Remove deletes an asset.
func (r *Release) Remove(asset string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.assets, asset)
}

// Inject makes later reads of the asset misbehave as the fault says.
func (r *Release) Inject(asset string, fault Fault) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.fault[asset] = fault
}

// Files returns a copy of every asset, for a test that serves the release from a directory.
func (r *Release) Files() map[string][]byte {
	r.mu.Lock()
	defer r.mu.Unlock()
	files := make(map[string][]byte, len(r.assets))
	for name, content := range r.assets {
		files[name] = bytes.Clone(content)
	}
	return files
}

// Opened lists the assets opened so far, in order.
func (r *Release) Opened() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.opened...)
}

// Open implements upgrade.Source.
func (r *Release) Open(_ context.Context, asset string) (io.ReadCloser, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.opened = append(r.opened, asset)
	content, ok := r.assets[asset]
	if !ok {
		return nil, fmt.Errorf("%w: %s", upgrade.ErrAssetNotFound, asset)
	}
	fault := r.fault[asset]
	switch {
	case fault.Fail:
		cut := min(fault.FailAfter, len(content))
		return io.NopCloser(io.MultiReader(bytes.NewReader(content[:cut]), failing{})), nil
	case fault.Truncate:
		return io.NopCloser(bytes.NewReader(content[:min(fault.TruncateAt, len(content))])), nil
	default:
		return io.NopCloser(bytes.NewReader(content)), nil
	}
}

type failing struct{}

func (failing) Read([]byte) (int, error) { return 0, ErrInjected }

// RoundTrip serves the release the way GitHub serves a release tag: GET <base>/<asset>, with 404
// for an asset the release lacks. It implements http.RoundTripper, so
// &http.Client{Transport: release} reads the release without a network.
func (r *Release) RoundTrip(request *http.Request) (*http.Response, error) {
	if request.Method != http.MethodGet {
		return nil, fmt.Errorf("upgradetest: unexpected %s request", request.Method)
	}
	asset := request.URL.Path[strings.LastIndex(request.URL.Path, "/")+1:]
	body, err := r.Open(request.Context(), asset)
	status := http.StatusOK
	if errors.Is(err, upgrade.ErrAssetNotFound) {
		status = http.StatusNotFound
		body = io.NopCloser(strings.NewReader("not found"))
	} else if err != nil {
		return nil, err
	}
	return &http.Response{
		StatusCode: status,
		Status:     fmt.Sprintf("%d %s", status, http.StatusText(status)),
		Header:     http.Header{},
		Body:       body,
		Request:    request,
	}, nil
}
