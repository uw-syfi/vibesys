// Package filestore implements store.Store on disk.
//
// It is the only code in the module that knows the physical layout: where state lives, the file
// names, and the encoding of each record. It keeps that encoding separate from the domain types
// in internal/core and the generated contract types in gen/. The layout rule in
// internal/archcheck keeps every other package from naming it. The per-user index of stores is
// not part of this package yet.
//
// # Layout
//
// A store is a directory, <repository>/.vibesys-state/<task> for OpenTask, holding one file per
// fact, each a JSON document that starts with its format version:
//
//	format                     the version of the layout
//	.owner.lock                empty; the owner holds a lock on it
//	.owner                     who owns the store, and how many owners it has had
//	jobs/<job>.json            one job, replaced whole on each change
//	results/<job>.json         the result a job saved, with its place in the order of saving
//	plans/<number>.json        one plan, replaced whole on each change
//	promotions/<n>.json        the nth promotion
//	runs/<run>/<n>.json        the nth event of a run
//	logs/<job>/<offset>        the bytes appended to a job's log at that offset, as written
//
// A file is never appended to or edited in place. The spec's first sketch kept the events of a
// run in one appended file; an append is not atomic on NFS, so each event is its own file.
//
// # Atomicity
//
// The package states which filesystem operations it relies on, so that the state directory may
// be on NFS (the socket may not be, and is not in this package). It uses these and no others:
// create a file and write it, fsync it, rename it within its directory, fsync the directory,
// create a directory (which fails if it exists), unlink, and read. Rename within a directory
// replaces the destination in one step on a local filesystem and on NFS. Nothing here depends on
// append, O_EXCL on a file, or a rename between directories, and only the Owners section below
// depends on file locking.
//
// A change is written to a temporary file in the directory it belongs to, fsynced, renamed to its
// final name, and the directory is fsynced. The rename is the change's commit point. A change
// that saves a result with a job's new state writes the result file first and the job file
// last; a result file whose job does not own it is the leftover of a crash between the two, and
// the next Open ignores it. After a crash the store is therefore in the state before the change
// or the state after it, and nothing earlier is lost.
//
// A failed commit does not leave the process believing something the disk does not hold: the
// store reads the directory again before it serves the next request, so the way back from a
// failure is the way back from a crash.
//
// # Owners
//
// A store has one owner, and the store is what enforces that, because the store is what two
// owners would damage and what they share: state may be on NFS and reached from several machines,
// and anything kept beside the socket is per machine, or per value of XDG_RUNTIME_DIR. Open does
// three things, and each covers what the one before cannot.
//
// The lock. Open takes a flock on .owner.lock, or fails. It ends with the process, however that
// ends, so a crashed owner leaves nothing to clean up. On NFS a client machine that crashed keeps
// it until its lease runs out or it reboots, and the store is refused until then with an error
// that names that owner. A holder with the opener's own place can only be a daemon on its way out,
// and that refusal is marked store.ErrOwnerLeaving. A filesystem where the lock would keep nobody out is refused: see
// fsys.OS.Lock for how that is found. There is no falling back to no lock.
//
// The record. A lock says nobody is here now. It does not say the last owner's work is over: its
// supervisors outlive it, and can only be found from where they started. So .owner names the
// machine and the runtime directory of the last owner, and a store with a running job whose
// record names another place is refused, because opening it would mark a live job lost. When that
// place is gone for good, removing the record is the way on, and the error says so.
//
// The fence. A lock can stop working under its holder: an NFS server can lose it while the client
// is cut off, a mount can hide it from other machines without any sign, and anyone can delete the
// file. So every open writes .owner again, under a new generation, and the owner reads the file
// before every commit and every reload, and stops if it is not what it wrote. That read needs no
// lock, and NFS shows a file renamed over another to the next open. It closes all but the last
// instant: a commit that passed the check while another owner took over still lands, once. The
// loser's next call fails, and it fails with FailureUnavailable, so that it holds everything it
// has and decides nothing.
//
// # Use
//
// The store serializes the requests of its one owner. A run name is a file name, so two run names
// that differ only in case share a directory on a case-insensitive filesystem.
//
// # Versions
//
// Open refuses a directory whose format file, or any record, has a newer version than this
// program reads, with an error of kind core.FailureIncompatible that names the file. It refuses
// a record it cannot decode, one with a field it does not know, and one whose name and content
// disagree, with an error of kind core.FailureInternal that names the file. It refuses a
// directory that holds other files and no format file. The .owner file is a record like any other.
package filestore
