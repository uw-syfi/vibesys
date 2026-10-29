package runlog

import (
	"bytes"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"io"
	"testing"
	"testing/quick"
)

func framed(payload []byte) []byte {
	b := binary.AppendUvarint(nil, uint64(len(payload)))
	b = binary.LittleEndian.AppendUint32(b, crc32.Checksum(payload, crc32.MakeTable(crc32.Castagnoli)))
	return append(b, payload...)
}

// A growing ReaderAt models arbitrary writer append boundaries without I/O.
type growing struct{ data []byte }

func (g *growing) ReadAt(p []byte, off int64) (int, error) {
	return bytes.NewReader(g.data).ReadAt(p, off)
}

func TestEveryAppendBoundary(t *testing.T) {
	property := func(records [][]byte) bool {
		all := []byte(Magic)
		ends := make([]int, len(records))
		for i, record := range records {
			all = append(all, framed(record)...)
			ends[i] = len(all)
		}
		source := &growing{}
		reader := NewReader(source)
		yielded := 0
		for end := 0; end <= len(all); end++ {
			source.data = all[:end]
			for {
				record, err := reader.Next()
				if errors.Is(err, io.EOF) {
					break
				}
				if err != nil || yielded >= len(records) || ends[yielded] > end || !bytes.Equal(record, records[yielded]) {
					return false
				}
				yielded++
			}
			expected := 0
			for _, boundary := range ends {
				if boundary <= end {
					expected++
				}
			}
			if yielded != expected {
				return false
			}
		}
		return yielded == len(records)
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

func TestPayloadCorruption(t *testing.T) {
	property := func(payload []byte, position uint) bool {
		if len(payload) == 0 {
			payload = []byte{0}
		}
		data := append([]byte(Magic), framed(payload)...)
		data[len(data)-len(payload)+int(position%uint(len(payload)))] ^= 1
		_, err := NewReader(bytes.NewReader(data)).Next()
		var corruption *Corruption
		return errors.As(err, &corruption)
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

func TestInvalidFrames(t *testing.T) {
	for name, data := range map[string][]byte{
		"magic":    []byte("VSRLOG02"),
		"overflow": append([]byte(Magic), bytes.Repeat([]byte{0xff}, 10)...),
		"oversize": append([]byte(Magic), binary.AppendUvarint(nil, MaxRecordBytes+1)...),
	} {
		t.Run(name, func(t *testing.T) {
			_, err := NewReader(bytes.NewReader(data)).Next()
			var corruption *Corruption
			if !errors.As(err, &corruption) {
				t.Fatalf("got %v, want Corruption", err)
			}
		})
	}
}

func FuzzReader(f *testing.F) {
	f.Add([]byte(Magic))
	f.Add(append([]byte(Magic), framed([]byte("payload"))...))
	f.Fuzz(func(t *testing.T, data []byte) {
		reader := NewReader(bytes.NewReader(data))
		for consumed := 0; consumed <= len(data); consumed++ {
			payload, err := reader.Next()
			if err != nil {
				return
			}
			if len(payload) > MaxRecordBytes {
				t.Fatal("unbounded payload")
			}
		}
		t.Fatal("reader did not advance")
	})
}
