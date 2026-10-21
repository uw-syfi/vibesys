package runlog_test

import (
	"bytes"
	"encoding/binary"
	"hash/crc32"
	"io"
	"os"
	"path/filepath"
	"testing"
	"testing/quick"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
)

type countedReader struct {
	source *bytes.Reader
	reads  int
}

func (r *countedReader) ReadAt(data []byte, offset int64) (int, error) {
	r.reads++
	return r.source.ReadAt(data, offset)
}

func indexedLog(t *testing.T, count int) ([]byte, os.FileInfo) {
	t.Helper()
	data := []byte(runlog.Magic)
	for seq := 1; seq <= count; seq++ {
		payload, err := proto.Marshal(&storev1.Record{Seq: uint64(seq)})
		if err != nil {
			t.Fatal(err)
		}
		data = binary.AppendUvarint(data, uint64(len(payload)))
		data = binary.LittleEndian.AppendUint32(data, crc32.Checksum(payload, crc32.MakeTable(crc32.Castagnoli)))
		data = append(data, payload...)
	}
	path := filepath.Join(t.TempDir(), "run.log")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return data, info
}

func TestSparseSeekingCountsReaderWork(t *testing.T) {
	const count = 4096
	data, info := indexedLog(t, count)
	source := &countedReader{source: bytes.NewReader(data)}
	reader := runlog.NewReader(source)
	index := &runlog.Index{}
	for {
		payload, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		seq, err := runlog.Sequence(payload)
		if err != nil {
			t.Fatal(err)
		}
		index.Observe("run.log", info, runlog.Position{ThroughSeq: seq, Offset: reader.Offset()})
	}
	property := func(input uint16) bool {
		from := uint64(input%count) + 1
		position := index.Before("run.log", info, from)
		source.reads = 0
		reader := runlog.NewReaderAt(source, position.Offset)
		next := position.ThroughSeq + 1
		for {
			payload, err := reader.Next()
			if err == io.EOF {
				break
			}
			if err != nil {
				return false
			}
			seq, err := runlog.Sequence(payload)
			if err != nil || seq != next {
				return false
			}
			next++
		}
		// Each frame has two ReaderAt calls. At most 127 preceding records are
		// replayed; constant work reads magic and tests EOF. No timing assertion.
		return next == count+1 && source.reads <= 2*(count-int(from)+1+128)+2
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

func TestSparseIndexInvalidatesChangedFiles(t *testing.T) {
	for _, replacement := range []bool{false, true} {
		t.Run(map[bool]string{false: "truncate", true: "replace"}[replacement], func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "run.log")
			if err := os.WriteFile(path, make([]byte, 1000), 0600); err != nil {
				t.Fatal(err)
			}
			before, err := os.Stat(path)
			if err != nil {
				t.Fatal(err)
			}
			index := &runlog.Index{}
			index.Observe(path, before, runlog.Position{ThroughSeq: 128, Offset: 1000})
			if position := index.Before(path, before, 129); position.ThroughSeq != 128 {
				t.Fatal(position)
			}
			if replacement {
				next := path + ".next"
				if err := os.WriteFile(next, make([]byte, 1000), 0600); err != nil {
					t.Fatal(err)
				}
				if err := os.Rename(next, path); err != nil {
					t.Fatal(err)
				}
			} else if err := os.WriteFile(path, []byte("short"), 0600); err != nil {
				t.Fatal(err)
			}
			after, err := os.Stat(path)
			if err != nil {
				t.Fatal(err)
			}
			if position := index.Before(path, after, 129); position.Offset != 0 || position.ThroughSeq != 0 {
				t.Fatal(position)
			}
		})
	}
}

func TestSeekStillVerifiesMagic(t *testing.T) {
	data, _ := indexedLog(t, 1)
	data[0] = 'X'
	_, err := runlog.NewReaderAt(bytes.NewReader(data), int64(len(runlog.Magic))).Next()
	if err == nil {
		t.Fatal("seek bypassed magic validation")
	}
}
