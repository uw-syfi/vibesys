// Package kubelease keeps leases in a Kubernetes cluster, as Lease objects of the API group
// coordination.k8s.io, so the daemons that share a cluster take turns at it whichever machines they
// run on. A lease is held for a duration and renewed while its daemon lives; a daemon that dies
// stops renewing it, and once the duration has passed since the last renewal the next daemon takes
// it over. A crashed holder never blocks the cluster for good.
//
// A daemon judges expiry by its own clock against the renewal time the holder wrote, so the
// clocks of the daemons that share a cluster are assumed to agree to well within the duration.
// Every write names the version of the object it read, so two daemons that find a lease expired
// at once cannot both take it: the API server refuses the second write.
package kubelease

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
)

// DefaultDuration is how long a lease lives after its last renewal unless the operator config says
// otherwise. A crashed holder blocks the cluster this long.
const DefaultDuration = 60 * time.Second

// HolderAnnotation is the annotation of a Lease object that names its holder, as a cliv1.LeaseRecord in
// JSON. The object's own holderIdentity is a token of the one lease, so that a renewal or a release
// never touches a lease taken after it.
const HolderAnnotation = "vibesys.dev/holder"

// API is what a Cluster needs of the API server: Lease objects of one namespace, read and written
// whole. Each call is bounded by the API itself.
type API interface {
	// Get reads the Lease named name, or returns nil when there is none.
	Get(ctx context.Context, name string) (*Object, error)
	// Create makes the Lease, and fails with ErrExists when one of its name is there.
	Create(ctx context.Context, o *Object) error
	// Replace writes the Lease over the version o.Metadata.ResourceVersion names, and fails with
	// ErrConflict when it is not that version any more.
	Replace(ctx context.Context, o *Object) error
	// Where names the cluster and the namespace in messages.
	Where() string
}

// The errors of an API.
var (
	ErrExists   = errors.New("the lease exists")
	ErrConflict = errors.New("the lease changed since it was read")
)

// Object is a Lease object, as much of it as a lease needs.
type Object struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`
	Metadata   Meta   `json:"metadata"`
	Spec       Spec   `json:"spec"`
}

// Meta is an Object's metadata.
type Meta struct {
	Name            string            `json:"name"`
	Namespace       string            `json:"namespace,omitempty"`
	ResourceVersion string            `json:"resourceVersion,omitempty"`
	Annotations     map[string]string `json:"annotations,omitempty"`
}

// Spec is a Lease's spec. A lease nobody holds has no holder identity.
type Spec struct {
	HolderIdentity       string     `json:"holderIdentity,omitempty"`
	LeaseDurationSeconds int32      `json:"leaseDurationSeconds,omitempty"`
	AcquireTime          *MicroTime `json:"acquireTime,omitempty"`
	RenewTime            *MicroTime `json:"renewTime,omitempty"`
}

// MicroTime is a time as the Kubernetes API writes a MicroTime.
type MicroTime struct{ time.Time }

const microFormat = "2006-01-02T15:04:05.000000Z07:00"

// MarshalJSON implements json.Marshaler.
func (t MicroTime) MarshalJSON() ([]byte, error) {
	return json.Marshal(t.UTC().Format(microFormat))
}

// UnmarshalJSON implements json.Unmarshaler.
func (t *MicroTime) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	parsed, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return err
	}
	t.Time = parsed
	return nil
}

// Cluster is the leases of a Kubernetes cluster.
type Cluster struct {
	API API
	// Duration is how long a lease lives after its last renewal; zero is DefaultDuration. It is
	// renewed every third of it.
	Duration time.Duration
	// Prefix comes before a key in the name of its Lease object; empty is "vibesys-".
	Prefix string
	Clock  clock.Clock
	Timers clock.Timers
	// Token makes the token that names one lease; nil uses random bytes.
	Token func() string
	// Logf hears of renewals that failed. nil drops them.
	Logf func(format string, args ...any)
}

var _ lease.Leases = Cluster{}

// Where implements lease.Leases.
func (c Cluster) Where() string { return c.API.Where() }

func (c Cluster) duration() time.Duration {
	if c.Duration <= 0 {
		return DefaultDuration
	}
	return c.Duration
}

func (c Cluster) name(key lease.Key) string {
	if c.Prefix == "" {
		return "vibesys-" + string(key)
	}
	return c.Prefix + string(key)
}

// attempts bounds the writes that lose a race with another daemon before Acquire gives up.
const attempts = 4

// Acquire implements lease.Leases.
func (c Cluster) Acquire(ctx context.Context, key lease.Key, holder core.LeaseHolder) (lease.Lease, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if _, err := lease.ParseKey(string(key)); err != nil {
		return nil, err
	}
	token := c.token()
	name := c.name(key)
	record, err := lease.EncodeRecord(holder)
	if err != nil {
		return nil, err
	}
	for range attempts {
		cur, err := c.API.Get(ctx, name)
		if err != nil {
			return nil, err
		}
		now := c.Clock.Now()
		if cur != nil && c.held(cur, now) {
			return nil, &lease.HeldError{Key: key, Where: c.Where(), Holder: holderOf(cur)}
		}
		o := &Object{
			APIVersion: "coordination.k8s.io/v1", Kind: "Lease",
			Metadata: Meta{Name: name, Annotations: map[string]string{HolderAnnotation: string(record)}},
			Spec: Spec{
				HolderIdentity: token, LeaseDurationSeconds: int32(c.duration() / time.Second),
				AcquireTime: &MicroTime{now}, RenewTime: &MicroTime{now},
			},
		}
		if cur == nil {
			err = c.API.Create(ctx, o)
		} else {
			o.Metadata.ResourceVersion = cur.Metadata.ResourceVersion
			o.Metadata.Annotations = merged(cur.Metadata.Annotations, string(record))
			err = c.API.Replace(ctx, o)
		}
		if errors.Is(err, ErrExists) || errors.Is(err, ErrConflict) {
			// Another daemon wrote it between the read and the write: read it again.
			continue
		}
		if err != nil {
			return nil, err
		}
		// The renewals outlive ctx, which bounds only the taking: Release and Abandon end them.
		life, end := context.WithCancel(context.Background())
		l := &Lease{c: c, key: key, name: name, token: token, renewed: now, lost: make(chan struct{}), life: life, end: end, done: make(chan struct{})}
		go l.renew()
		return l, nil
	}
	return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("lease: the lease %s on %s kept changing under %d attempts to take it", key, c.Where(), attempts)}
}

func (c Cluster) token() string {
	if c.Token != nil {
		return c.Token()
	}
	return randomToken()
}

// held says whether a Lease object is held at now: it names a holder, and its last renewal is
// within its duration.
func (c Cluster) held(o *Object, now time.Time) bool {
	if o.Spec.HolderIdentity == "" {
		return false
	}
	last := o.Spec.RenewTime
	if last == nil {
		last = o.Spec.AcquireTime
	}
	if last == nil {
		return false
	}
	d := time.Duration(o.Spec.LeaseDurationSeconds) * time.Second
	if d <= 0 {
		d = c.duration()
	}
	return now.Before(last.Add(d))
}

func holderOf(o *Object) core.LeaseHolder {
	h, err := lease.DecodeRecord([]byte(o.Metadata.Annotations[HolderAnnotation]))
	if err != nil {
		return lease.Anonymous()
	}
	return h
}

// merged keeps the annotations others put on the object, with the holder's set to record, or
// removed when record is empty.
func merged(annotations map[string]string, record string) map[string]string {
	out := map[string]string{}
	for k, v := range annotations {
		out[k] = v
	}
	if record == "" {
		delete(out, HolderAnnotation)
	} else {
		out[HolderAnnotation] = record
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// Lease is a lease of a cluster, renewed until it is released or lost.
type Lease struct {
	c     Cluster
	key   lease.Key
	name  string
	token string

	mu       sync.Mutex
	renewed  time.Time
	released bool
	lost     chan struct{}
	life     context.Context
	end      context.CancelFunc
	done     chan struct{}
}

var _ lease.Lease = (*Lease)(nil)

// errTaken says the lease is not this one's any more.
var errTaken = errors.New("the lease was taken over")

// renew renews the lease every third of its duration, until it is released, abandoned or lost. A
// renewal that fails is tried again at the next period; the lease is lost when it is taken over,
// or when its duration has passed since the last renewal that reached the cluster.
func (l *Lease) renew() {
	defer close(l.done)
	period := l.c.duration() / 3
	for {
		if !clock.Sleep(l.life, l.c.Timers, period) {
			return
		}
		err := l.renewOnce(l.life)
		if l.life.Err() != nil {
			return
		}
		if err == nil {
			continue
		}
		l.logf("lease %s on %s: renewing: %v", l.key, l.c.Where(), err)
		l.mu.Lock()
		expired := !l.c.Clock.Now().Before(l.renewed.Add(l.c.duration()))
		l.mu.Unlock()
		if errors.Is(err, errTaken) || expired {
			close(l.lost)
			return
		}
	}
}

func (l *Lease) renewOnce(ctx context.Context) error {
	cur, err := l.c.API.Get(ctx, l.name)
	if err != nil {
		return err
	}
	if cur == nil || cur.Spec.HolderIdentity != l.token {
		return errTaken
	}
	now := l.c.Clock.Now()
	cur.Spec.RenewTime = &MicroTime{now}
	if err := l.c.API.Replace(ctx, cur); err != nil {
		return err
	}
	l.mu.Lock()
	l.renewed = now
	l.mu.Unlock()
	return nil
}

func randomToken() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func (l *Lease) logf(format string, args ...any) {
	if l.c.Logf != nil {
		l.c.Logf(format, args...)
	}
}

// halt stops the renewals and waits for them to end. It reports whether this call stopped them.
func (l *Lease) halt() bool {
	l.mu.Lock()
	if l.released {
		l.mu.Unlock()
		return false
	}
	l.released = true
	l.mu.Unlock()
	l.end()
	<-l.done
	return true
}

// Release implements lease.Lease. It stops the renewals and clears the holder of the object, if
// the lease is still this one's; a release that does not reach the cluster leaves the lease to
// expire.
func (l *Lease) Release(ctx context.Context) error {
	if !l.halt() {
		return nil
	}
	for range attempts {
		cur, err := l.c.API.Get(ctx, l.name)
		if err != nil {
			return err
		}
		if cur == nil || cur.Spec.HolderIdentity != l.token {
			return nil
		}
		cur.Spec.HolderIdentity = ""
		cur.Spec.RenewTime = &MicroTime{l.c.Clock.Now()}
		cur.Metadata.Annotations = merged(cur.Metadata.Annotations, "")
		err = l.c.API.Replace(ctx, cur)
		if errors.Is(err, ErrConflict) {
			continue
		}
		return err
	}
	return nil
}

// Abandon stops the renewals without giving the lease back, as the death of its daemon does: the
// lease expires a duration after its last renewal.
func (l *Lease) Abandon() { l.halt() }

// Lost implements lease.Lease.
func (l *Lease) Lost() <-chan struct{} { return l.lost }

// Key implements lease.Lease.
func (l *Lease) Key() lease.Key { return l.key }
