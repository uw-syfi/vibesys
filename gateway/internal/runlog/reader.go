// Package runlog reads append-only VSRLOG01 files without consuming torn tails.
package runlog

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"hash/crc32"
	"io"

	"github.com/uw-syfi/vibesys/gateway/gen"
)

const Magic = gen.RunLogMagic

// MaxRecordBytes bounds memory even when a damaged prefix declares a huge length.
const MaxRecordBytes = gen.RunLogMaxRecordBytes

var castagnoli = crc32.MakeTable(crc32.Castagnoli)

// Corruption identifies a complete invalid frame. EOF instead means more bytes
// may complete the current frame; callers may retry at the same offset.
type Corruption struct {
	Offset int64
	Reason string
}

func (e *Corruption) Error() string {
	if e.Offset < 0 {
		return "run.log corruption: " + e.Reason
	}
	return fmt.Sprintf("run.log corruption at byte %d: %s", e.Offset, e.Reason)
}

// Reader advances only after a complete, checksum-verified record. The source
// must retain its existing bytes; concurrent appends are supported.
type Reader struct {
	source    io.ReaderAt
	offset    int64
	magicRead bool
}

func NewReader(source io.ReaderAt) *Reader { return &Reader{source: source} }

// Next returns the original payload, io.EOF for an incomplete tail, or an error.
func (r *Reader) Next() ([]byte, error) {
	if !r.magicRead {
		var magic [len(Magic)]byte
		if _, err := r.source.ReadAt(magic[:], 0); err != nil {
			return nil, err
		}
		if !bytes.Equal(magic[:], []byte(Magic)) {
			return nil, r.corrupt("invalid magic")
		}
		r.magicRead = true
		if r.offset == 0 {
			r.offset = int64(len(Magic))
		}
	}
	var prefix [binary.MaxVarintLen64]byte
	n, err := r.source.ReadAt(prefix[:], r.offset)
	if err != nil && err != io.EOF {
		return nil, err
	}
	length, width := binary.Uvarint(prefix[:n])
	if width == 0 {
		if n == binary.MaxVarintLen64 {
			return nil, r.corrupt("length varint overflow")
		}
		return nil, io.EOF
	}
	if width < 0 {
		return nil, r.corrupt("length varint overflow")
	}
	if length > MaxRecordBytes {
		return nil, r.corrupt(fmt.Sprintf("record exceeds %d byte limit", MaxRecordBytes))
	}
	frame := make([]byte, 4+int(length))
	if _, err := r.source.ReadAt(frame, r.offset+int64(width)); err != nil {
		return nil, err
	}
	payload := frame[4:]
	if binary.LittleEndian.Uint32(frame[:4]) != crc32.Checksum(payload, castagnoli) {
		return nil, r.corrupt("CRC32C mismatch")
	}
	r.offset += int64(width) + int64(len(frame))
	return payload, nil
}

func (r *Reader) corrupt(reason string) error {
	return &Corruption{Offset: r.offset, Reason: reason}
}
