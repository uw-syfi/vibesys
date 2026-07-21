package accuracy

import (
	"context"
	"errors"
	"fmt"
	"sync"
)

type CleanupFunc func(context.Context) error

type journalEntry struct {
	name    string
	cleanup CleanupFunc
	active  bool
}

// Journal records ownership of fixture mutations. Callers should record before
// issuing a mutation whose transport outcome could be ambiguous. Cleanup runs
// in reverse order, continues after errors, and keeps failed entries active so
// a caller can retry cleanup rather than silently losing ownership.
type Journal struct {
	mu      sync.Mutex
	entries []*journalEntry
	byName  map[string]*journalEntry
}

func NewJournal() *Journal {
	return &Journal{byName: make(map[string]*journalEntry)}
}

func (j *Journal) Record(name string, cleanup CleanupFunc) error {
	if name == "" {
		return fmt.Errorf("cleanup entry name must not be empty")
	}
	if cleanup == nil {
		return fmt.Errorf("cleanup entry %q has no cleanup function", name)
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	if existing, duplicate := j.byName[name]; duplicate && existing.active {
		return fmt.Errorf("cleanup entry %q is already active", name)
	}
	entry := &journalEntry{name: name, cleanup: cleanup, active: true}
	j.entries = append(j.entries, entry)
	j.byName[name] = entry
	return nil
}

func (j *Journal) Dismiss(name string) error {
	j.mu.Lock()
	defer j.mu.Unlock()
	entry, exists := j.byName[name]
	if !exists || !entry.active {
		return fmt.Errorf("cleanup entry %q is not active", name)
	}
	entry.active = false
	return nil
}

func (j *Journal) Cleanup(ctx context.Context) error {
	j.mu.Lock()
	entries := append([]*journalEntry(nil), j.entries...)
	j.mu.Unlock()

	var cleanupErrors []error
	for index := len(entries) - 1; index >= 0; index-- {
		if err := ctx.Err(); err != nil {
			cleanupErrors = append(cleanupErrors, fmt.Errorf("cleanup deadline: %w", err))
			break
		}
		entry := entries[index]
		j.mu.Lock()
		active := entry.active
		j.mu.Unlock()
		if !active {
			continue
		}
		if err := entry.cleanup(ctx); err != nil {
			cleanupErrors = append(cleanupErrors, fmt.Errorf("cleanup %q: %w", entry.name, err))
			continue
		}
		j.mu.Lock()
		entry.active = false
		j.mu.Unlock()
	}
	return errors.Join(cleanupErrors...)
}

func (j *Journal) Active() int {
	j.mu.Lock()
	defer j.mu.Unlock()
	count := 0
	for _, entry := range j.entries {
		if entry.active {
			count++
		}
	}
	return count
}
