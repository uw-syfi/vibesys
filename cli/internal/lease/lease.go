// Package lease declares Leases: where the daemons that share a resource take turns at holding it
// alone. The scheduler of one daemon keeps its own jobs from sharing an exclusive claim; a lease
// keeps the jobs of every daemon from doing so, whoever runs them and on whichever machine: other
// users of the same machine, or daemons on other machines that share a cluster or an ssh host.
//
// A lease is taken for an exclusive claim before its job starts and given back when the job's hold
// ends. A shared claim takes none. Every implementation gives a lease back on every path: by
// Release, and, when its holder dies without one, by the operating system or by its expiry, so a
// crashed holder never blocks the resource for good. Each one says which.
//
// Implementations are packages of their own under this one, and import this package and core,
// never each other. Only the binary's wiring says which resource is leased where (a Book), so the
// core never branches on a kind of resource. Every implementation passes the contract in leasetest.
package lease

import (
	"bytes"
	"context"
	"fmt"
	"regexp"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Leases is a place where leases are kept, such as a directory of this machine, a host reached over
// ssh, or a Kubernetes cluster. Two daemons that share a resource reach the same Leases for it.
type Leases interface {
	// Acquire takes the lease named key for holder, without waiting for it. It fails with a
	// *HeldError naming the holder when another one holds it, which may be this daemon under
	// another job. A failure of any other kind leaves nothing held. ctx bounds the attempt and
	// not the lease, which is held until Release, or until it is lost.
	Acquire(ctx context.Context, key Key, holder core.LeaseHolder) (Lease, error)
	// Where names the place in messages, such as "this machine" or "the cluster kind-a".
	Where() string
}

// Lease is a lease that is held.
type Lease interface {
	// Release gives the lease back. It may be called more than once, and from any path; after the
	// first call it does nothing. ctx bounds how long it waits for the place to say so; a lease
	// whose release did not reach the place is given back by its expiry, or when its holder dies.
	Release(ctx context.Context) error
	// Lost is closed when the lease stops being held without a Release, such as a renewal that did
	// not reach the cluster before the lease expired, or a connection to its host that dropped. It
	// is never closed for a lease that cannot be lost.
	Lost() <-chan struct{}
	// Key is the lease's name.
	Key() Key
}

// HeldError says a lease is held by someone else, and who.
type HeldError struct {
	Key    Key
	Where  string
	Holder core.LeaseHolder
}

func (e *HeldError) Error() string {
	return fmt.Sprintf("the lease %s on %s is held by %s", e.Key, e.Where, e.Holder)
}

// Key names a lease: the thing that is held alone, such as a machine or a cluster. It is a file
// name and the name of a Kubernetes object, so it is short, lower-case letters, digits, '.' and
// '-', starting and ending with a letter or a digit.
type Key string

var keyForm = regexp.MustCompile(`^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$`)

// ParseKey refuses a name that is not a Key, with a FailureInvalidInput naming it.
func ParseKey(name string) (Key, error) {
	if !keyForm.MatchString(name) {
		return "", &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("lease: %q is not a lease name (lower-case letters, digits, '.' and '-', at most 63)", name)}
	}
	return Key(name), nil
}

// Place is where a resource is leased: the Leases that keep its lease, and the lease's name there.
type Place struct {
	Leases Leases
	Key    Key
}

// Book says where each resource is leased. A resource it has no place for is not leased: the
// scheduler of this daemon is the only one to arbitrate it, or something else does, such as
// Slurm's exclusive allocation.
type Book interface {
	Place(resource string) (Place, bool)
}

// Map is a Book of fixed places, by resource id.
type Map map[string]Place

// Place implements Book.
func (m Map) Place(resource string) (Place, bool) {
	p, ok := m[resource]
	return p, ok
}

// NoBook leases nothing. It is the Book of a daemon that shares no resource with another.
type NoBook struct{}

// Place implements Book.
func (NoBook) Place(string) (Place, bool) { return Place{}, false }

// EncodeRecord writes the holder of a lease as the record kept beside it, so whoever finds the lease
// held can say by whom. It is one line of JSON, the protojson of cliv1.LeaseRecord, which keeps the
// keys the first builds wrote: daemons of different builds share these records, and a record of an
// older build reads here while a record of a newer one, with fields added, reads in an older build.
func EncodeRecord(h core.LeaseHolder) ([]byte, error) { return wire.EncodeLeaseRecord(h) }

// DecodeRecord reads the holder out of a kept record. A record that is empty or is not one is an
// error of kind invalid input, never a holder: the lock is the lease and the record only names its
// holder, so a caller that finds a held lease with an unreadable record reports it held by nobody
// it can name (see Anonymous), and never as free.
func DecodeRecord(data []byte) (core.LeaseHolder, error) {
	return wire.DecodeLeaseRecord(bytes.TrimSpace(data))
}

// Anonymous is the holder named by a record that could not be read: nobody in particular.
func Anonymous() core.LeaseHolder { return core.LeaseHolder{} }
