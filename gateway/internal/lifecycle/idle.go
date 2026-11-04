// Package lifecycle owns the gateway's connection-based idle policy.
package lifecycle

// Idle tracks connections and invalidates pending expiry events on every
// transition. A zero value is an idle server at generation zero.
type Idle struct {
	Connections int
	Generation  uint64
}

func (s Idle) Opened() Idle {
	return Idle{Connections: s.Connections + 1, Generation: s.Generation + 1}
}

func (s Idle) Closed() Idle {
	if s.Connections == 0 {
		panic("connection closed without being opened")
	}
	return Idle{Connections: s.Connections - 1, Generation: s.Generation + 1}
}

// Expired accepts only the current idle generation, so a late timer cannot
// shut down a server that accepted a connection after that timer was armed.
func (s Idle) Expired(generation uint64) bool {
	return s.Connections == 0 && generation == s.Generation
}
