// Package client is how the command line tool talks to the daemon: it finds the daemon's socket,
// starts a daemon when none runs, checks the two builds can talk, and offers the daemon's calls
// as methods over the contract's messages.
//
// It works in the contract's messages and not in the domain's, so the command line, which only
// reads flags and prints, never needs the domain. Everything a user can wait for is done over the
// daemon's streams; there is no polling.
package client
