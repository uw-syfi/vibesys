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
// file locking, append, O_EXCL on a file, or a rename between directories.
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
// # Use
//
// The store is for one process at a time. It serializes the requests of its own process and
// does nothing about a second process on the same directory; the daemon is what keeps that from
// happening. A run name is a file name, so two run names that differ only in case share a
// directory on a case-insensitive filesystem.
//
// # Versions
//
// Open refuses a directory whose format file, or any record, has a newer version than this
// program reads, with an error of kind core.FailureIncompatible that names the file. It refuses
// a record it cannot decode, one with a field it does not know, and one whose name and content
// disagree, with an error of kind core.FailureInternal that names the file. It refuses a
// directory that holds other files and no format file.
package filestore
