package runlog

import (
	"errors"
	"io"
)

// ReadPrefix passes every record payload of the published prefix to visit, in order.
// It rejects a damaged log, a non-increasing sequence, and a log shorter than its fence.
func ReadPrefix(source io.ReaderAt, committed *uint64, visit func(payload []byte) error) error {
	return ReadPrefixAt(source, Position{}, committed, visit)
}

// ReadPrefixAt validates the suffix after an authoritative checkpoint against the same commit fence.
func ReadPrefixAt(source io.ReaderAt, position Position, committed *uint64, visit func(payload []byte) error) error {
	if committed != nil && position.ThroughSeq > *committed {
		return &Corruption{Offset: position.Offset, Reason: "checkpoint exceeds committed sequence"}
	}
	if committed != nil && position.ThroughSeq == *committed {
		return nil
	}
	reader := NewReaderAt(source, position.Offset)
	through, seen := position.ThroughSeq, position.Offset > 0
	for {
		payload, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
		seq, err := Sequence(payload)
		if err != nil {
			return err
		}
		if seen && seq <= through {
			return &Corruption{Offset: -1, Reason: "non-increasing Record.seq"}
		}
		if committed != nil && seq > *committed {
			break
		}
		if err := visit(payload); err != nil {
			return err
		}
		through, seen = seq, true
		if committed != nil && through == *committed {
			break
		}
	}
	if committed != nil && (!seen || through < *committed) {
		return &Corruption{Offset: -1, Reason: "committed sequence exceeds log prefix"}
	}
	return nil
}
