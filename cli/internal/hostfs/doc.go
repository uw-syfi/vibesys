// Package hostfs is the part of the host's file system a check or a measure job touches: a
// scratch directory for the files a benchmark writes, and reading them back.
//
// FS is the interface. OS is the real one, and Memory is a fake that keeps files in a map so
// that a simulated host needs no disk. Both pass one contract suite.
package hostfs
