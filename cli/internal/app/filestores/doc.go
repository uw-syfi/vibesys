// Package filestores is the daemon's Stores over files: one filestore per task, in the
// repository's state directory, and the per-user index that lets a restarted daemon find them.
//
// It is the one place the daemon learns where stores are. The layout under a repository is
// internal/store/filestore's to know, and the index is its own small store of pointers.
package filestores
