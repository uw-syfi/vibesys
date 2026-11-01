// Package runtimedir is the per-user directory where the daemon keeps what only a running
// system has: its socket, its locks, and the directories of its supervisors.
//
// The directory is private to the user (mode 0700, owned by them) and on local disk. Unix
// sockets and file locks do not work reliably over a network file system, so a directory that
// is on one is refused with the way out named, and never used with a hope that it will do. It is
// not the place for anything that must survive a reboot: that is the store.
package runtimedir
