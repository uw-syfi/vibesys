// Package kubefake is an API server of Lease objects in memory, for the tests of kubelease: it
// keeps the versions of objects as the real one does, refuses a write over a version that is not
// the current one, and can be taken down.
package kubefake

import (
	"context"
	"errors"
	"strconv"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/lease/kubelease"
)

// ErrDown is the answer of a server that is down.
var ErrDown = errors.New("the API server does not answer")

// Server is an API server. Its zero value holds no object.
type Server struct {
	mu      sync.Mutex
	objects map[string]kubelease.Object
	version int
	down    bool
}

var _ kubelease.API = (*Server)(nil)

// Down takes the server down, or brings it back.
func (s *Server) Down(down bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.down = down
}

// Expire moves the last renewal of the Lease named name back past its duration, as the time since
// a crashed holder's last renewal would.
func (s *Server) Expire(name string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	o, ok := s.objects[name]
	if !ok || o.Spec.RenewTime == nil {
		return
	}
	back := time.Duration(o.Spec.LeaseDurationSeconds)*time.Second + time.Second
	o.Spec.RenewTime = &kubelease.MicroTime{Time: o.Spec.RenewTime.Add(-back)}
	if o.Spec.AcquireTime != nil {
		o.Spec.AcquireTime = &kubelease.MicroTime{Time: o.Spec.AcquireTime.Add(-back)}
	}
	s.put(o)
}

// Clear has the Lease named name held by nobody, as another daemon that took it over after its
// expiry and gave it back would leave it.
func (s *Server) Clear(name string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	o, ok := s.objects[name]
	if !ok {
		return
	}
	o.Spec.HolderIdentity = ""
	s.put(o)
}

// Where implements kubelease.API.
func (*Server) Where() string { return "the fake cluster" }

// Get implements kubelease.API.
func (s *Server) Get(_ context.Context, name string) (*kubelease.Object, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.down {
		return nil, ErrDown
	}
	o, ok := s.objects[name]
	if !ok {
		return nil, nil
	}
	o.Metadata.Annotations = clone(o.Metadata.Annotations)
	return &o, nil
}

// Create implements kubelease.API.
func (s *Server) Create(_ context.Context, o *kubelease.Object) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.down {
		return ErrDown
	}
	if _, ok := s.objects[o.Metadata.Name]; ok {
		return kubelease.ErrExists
	}
	s.put(*o)
	return nil
}

// Replace implements kubelease.API.
func (s *Server) Replace(_ context.Context, o *kubelease.Object) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.down {
		return ErrDown
	}
	cur, ok := s.objects[o.Metadata.Name]
	if !ok {
		return errors.New("not found")
	}
	if cur.Metadata.ResourceVersion != o.Metadata.ResourceVersion {
		return kubelease.ErrConflict
	}
	s.put(*o)
	return nil
}

func (s *Server) put(o kubelease.Object) {
	if s.objects == nil {
		s.objects = map[string]kubelease.Object{}
	}
	s.version++
	o.Metadata.ResourceVersion = strconv.Itoa(s.version)
	o.Metadata.Annotations = clone(o.Metadata.Annotations)
	s.objects[o.Metadata.Name] = o
}

func clone(m map[string]string) map[string]string {
	if m == nil {
		return nil
	}
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}
