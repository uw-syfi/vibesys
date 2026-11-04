//go:build kind

package kubelease_test

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/kubelease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/leasetest"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// The kind tag runs the contract against a real API server, such as a kind cluster's. The cluster
// is named by these variables; the test is skipped without them.
const (
	kubeconfigVar = "VIBESYS_KIND_KUBECONFIG"
	contextVar    = "VIBESYS_KIND_CONTEXT"
)

// kindDuration is short, so a crash expires within seconds.
const kindDuration = 3 * time.Second

func TestTheLeasesOfARealClusterKeepTheContract(t *testing.T) {
	kubeconfig, kubecontext := os.Getenv(kubeconfigVar), os.Getenv(contextVar)
	if kubeconfig == "" {
		t.Skipf("%s names no kubeconfig of a cluster to test against", kubeconfigVar)
	}
	api := kubelease.Kubectl{Shell: remote.Local{}, Kubeconfig: kubeconfig, Context: kubecontext, Timers: clock.System{}}
	leasetest.Run(t, leasetest.Contract{Seeds: 2, New: func(tb leasetest.TB) leasetest.World {
		prefix := "vibesys-test-" + random() + "-"
		tb.Cleanup(func() { deleteLeases(api, prefix) })
		b := &breakable{API: api}
		c := kubelease.Cluster{API: b, Duration: kindDuration, Prefix: prefix, Clock: clock.System{}, Timers: clock.System{}}
		return leasetest.World{
			Daemon: func() lease.Leases { return c },
			Crash: func(l lease.Lease) {
				l.(*kubelease.Lease).Abandon()
				clock.Sleep(context.Background(), clock.System{}, kindDuration+time.Second)
			},
			Lose: func(l lease.Lease) {
				// The cluster stops answering this daemon: its renewals fail until the lease expires.
				b.down(true)
				defer b.down(false)
				// The bound only guards against a renewal that never says the lease was lost.
				hang, done := context.WithTimeout(context.Background(), 10*kindDuration)
				defer done()
				select {
				case <-l.Lost():
				case <-hang.Done():
				}
			},
		}
	}})
}

// breakable is an API a test can cut off.
type breakable struct {
	kubelease.API
	mu  sync.Mutex
	off bool
}

func (b *breakable) down(off bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.off = off
}

func (b *breakable) cut() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.off {
		return errors.New("cut off")
	}
	return nil
}

func (b *breakable) Get(ctx context.Context, name string) (*kubelease.Object, error) {
	if err := b.cut(); err != nil {
		return nil, err
	}
	return b.API.Get(ctx, name)
}

func (b *breakable) Replace(ctx context.Context, o *kubelease.Object) error {
	if err := b.cut(); err != nil {
		return err
	}
	return b.API.Replace(ctx, o)
}

func random() string {
	b := make([]byte, 4)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func deleteLeases(api kubelease.Kubectl, prefix string) {
	args := []string{"--kubeconfig", api.Kubeconfig}
	if api.Context != "" {
		args = append(args, "--context", api.Context)
	}
	out, err := exec.Command("kubectl", append(args, "--namespace", "default", "get", "leases", "--output", "name")...).Output()
	if err != nil {
		return
	}
	for _, name := range strings.Fields(string(out)) {
		if strings.HasPrefix(strings.TrimPrefix(name, "lease.coordination.k8s.io/"), prefix) {
			_ = exec.Command("kubectl", append(args, "--namespace", "default", "delete", name)...).Run()
		}
	}
}
