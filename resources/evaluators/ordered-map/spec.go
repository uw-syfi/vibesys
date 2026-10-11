package main

import (
	"bytes"
	"fmt"
	"sort"
)

type mapEntry struct {
	key   []byte
	value []byte
}

type orderedMapSpec struct {
	entries []mapEntry
}

func (s *orderedMapSpec) put(key, value []byte) {
	index, found := s.search(key)
	stored := mapEntry{
		key:   append([]byte(nil), key...),
		value: append([]byte(nil), value...),
	}
	if found {
		s.entries[index] = stored
		return
	}
	s.entries = append(s.entries, mapEntry{})
	copy(s.entries[index+1:], s.entries[index:])
	s.entries[index] = stored
}

func (s *orderedMapSpec) get(key []byte) ([]byte, bool) {
	index, found := s.search(key)
	if !found {
		return nil, false
	}
	return append([]byte(nil), s.entries[index].value...), true
}

func (s *orderedMapSpec) remove(key []byte) ([]byte, bool) {
	index, found := s.search(key)
	if !found {
		return nil, false
	}
	value := append([]byte(nil), s.entries[index].value...)
	s.entries = append(s.entries[:index], s.entries[index+1:]...)
	return value, true
}

func (s *orderedMapSpec) min() (mapEntry, bool) {
	if len(s.entries) == 0 {
		return mapEntry{}, false
	}
	return cloneEntry(s.entries[0]), true
}

func (s *orderedMapSpec) max() (mapEntry, bool) {
	if len(s.entries) == 0 {
		return mapEntry{}, false
	}
	return cloneEntry(s.entries[len(s.entries)-1]), true
}

func (s *orderedMapSpec) predecessor(key []byte) (mapEntry, bool) {
	index := sort.Search(len(s.entries), func(i int) bool {
		return bytes.Compare(s.entries[i].key, key) >= 0
	})
	if index == 0 {
		return mapEntry{}, false
	}
	return cloneEntry(s.entries[index-1]), true
}

func (s *orderedMapSpec) successor(key []byte) (mapEntry, bool) {
	index := sort.Search(len(s.entries), func(i int) bool {
		return bytes.Compare(s.entries[i].key, key) > 0
	})
	if index == len(s.entries) {
		return mapEntry{}, false
	}
	return cloneEntry(s.entries[index]), true
}

func (s *orderedMapSpec) rangeQuery(start, end []byte, maxItems int) ([]mapEntry, bool) {
	begin := sort.Search(len(s.entries), func(i int) bool {
		return bytes.Compare(s.entries[i].key, start) >= 0
	})
	stop := sort.Search(len(s.entries), func(i int) bool {
		return bytes.Compare(s.entries[i].key, end) >= 0
	})
	if begin > stop {
		begin = stop
	}
	matched := s.entries[begin:stop]
	if maxItems < 0 {
		maxItems = 0
	}
	remaining := len(matched) > maxItems
	if len(matched) > maxItems {
		matched = matched[:maxItems]
	}
	cloned := make([]mapEntry, len(matched))
	for index, entry := range matched {
		cloned[index] = cloneEntry(entry)
	}
	return cloned, remaining
}

func (s *orderedMapSpec) search(key []byte) (int, bool) {
	index := sort.Search(len(s.entries), func(i int) bool {
		return bytes.Compare(s.entries[i].key, key) >= 0
	})
	if index < len(s.entries) && bytes.Equal(s.entries[index].key, key) {
		return index, true
	}
	return index, false
}

func (s *orderedMapSpec) clone() *orderedMapSpec {
	cloned := &orderedMapSpec{entries: make([]mapEntry, len(s.entries))}
	for index, entry := range s.entries {
		cloned.entries[index] = cloneEntry(entry)
	}
	return cloned
}

func specEqual(first, second *orderedMapSpec) bool {
	if len(first.entries) != len(second.entries) {
		return false
	}
	for index := range first.entries {
		if !bytes.Equal(first.entries[index].key, second.entries[index].key) ||
			!bytes.Equal(first.entries[index].value, second.entries[index].value) {
			return false
		}
	}
	return true
}

func cloneEntry(entry mapEntry) mapEntry {
	return mapEntry{
		key:   append([]byte(nil), entry.key...),
		value: append([]byte(nil), entry.value...),
	}
}

func applySpec(spec *orderedMapSpec, req request) (response, error) {
	switch req.operation {
	case operationPut:
		spec.put(req.key, req.value)
		return response{status: statusOK}, nil
	case operationGet:
		value, found := spec.get(req.key)
		if !found {
			return response{status: statusMissing}, nil
		}
		return response{status: statusOK, value: value}, nil
	case operationRemove:
		value, found := spec.remove(req.key)
		if !found {
			return response{status: statusMissing}, nil
		}
		return response{status: statusOK, value: value}, nil
	case operationMin:
		entry, found := spec.min()
		if !found {
			return response{status: statusMissing}, nil
		}
		return response{status: statusOK, key: entry.key, value: entry.value}, nil
	case operationMax:
		entry, found := spec.max()
		if !found {
			return response{status: statusMissing}, nil
		}
		return response{status: statusOK, key: entry.key, value: entry.value}, nil
	case operationPredecessor:
		entry, found := spec.predecessor(req.key)
		if !found {
			return response{status: statusMissing}, nil
		}
		return response{status: statusOK, key: entry.key, value: entry.value}, nil
	case operationSuccessor:
		entry, found := spec.successor(req.key)
		if !found {
			return response{status: statusMissing}, nil
		}
		return response{status: statusOK, key: entry.key, value: entry.value}, nil
	case operationRange:
		items, remaining := spec.rangeQuery(req.key, req.value, int(req.extra))
		converted := make([]rangeItem, len(items))
		for index, item := range items {
			converted[index] = rangeItem{key: item.key, value: item.value}
		}
		return response{status: statusOK, items: converted, remaining: remaining}, nil
	default:
		return response{}, fmt.Errorf("unknown spec operation %d", req.operation)
	}
}

func responsesMatch(expected, actual response, req request) error {
	if expected.status != actual.status {
		return fmt.Errorf("status %d, want %d", actual.status, expected.status)
	}
	if req.operation == operationRange {
		if expected.remaining != actual.remaining {
			return fmt.Errorf("remaining %v, want %v", actual.remaining, expected.remaining)
		}
		if len(expected.items) != len(actual.items) {
			return fmt.Errorf("range count %d, want %d", len(actual.items), len(expected.items))
		}
		for index := range expected.items {
			if !bytes.Equal(expected.items[index].key, actual.items[index].key) ||
				!bytes.Equal(expected.items[index].value, actual.items[index].value) {
				return fmt.Errorf("range item %d mismatch", index)
			}
		}
		return nil
	}
	if !bytes.Equal(expected.key, actual.key) {
		return fmt.Errorf("key %x, want %x", actual.key, expected.key)
	}
	if !bytes.Equal(expected.value, actual.value) {
		return fmt.Errorf("value %x, want %x", actual.value, expected.value)
	}
	return nil
}
