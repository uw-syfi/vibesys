// Package files reads and writes loadgen's files: a FileHeader, then the messages of the file's
// kind, length-delimited (binpb) or as proto3 JSON, one message per line (json). See
// proto/vibesys/evaluator/v1/loadgen.proto for each kind's layout.
package files

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"google.golang.org/protobuf/encoding/protodelim"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
)

// SchemaVersion is the one schema version this build reads and writes.
const SchemaVersion = 1

// maxMessageBytes bounds one message: a result with kept bodies is the largest.
const maxMessageBytes = 256 << 20

// Format is a file encoding.
type Format int

const (
	// Binpb is length-delimited binary protobuf, the default.
	Binpb Format = iota
	// JSON is proto3 JSON, one message per line.
	JSON
)

// ParseFormat parses a --format value.
func ParseFormat(value string) (Format, error) {
	switch value {
	case "binpb":
		return Binpb, nil
	case "json":
		return JSON, nil
	default:
		return 0, fmt.Errorf("--format must be binpb or json, got %q", value)
	}
}

// ErrSchemaVersion means a file's schema version is one this build does not implement.
var ErrSchemaVersion = errors.New("unsupported schema version")

// ErrMalformed means a file is not a valid file of the expected kind.
var ErrMalformed = errors.New("malformed file")

// Header returns the header loadgen writes for a file of this kind.
func Header(kind pb.FileKind, writer string) *pb.FileHeader {
	return &pb.FileHeader{SchemaVersion: SchemaVersion, Kind: kind, Writer: writer}
}

// Append appends one encoded message to buf.
func Append(buf []byte, format Format, message proto.Message) ([]byte, error) {
	switch format {
	case JSON:
		encoded, err := protojson.Marshal(message)
		if err != nil {
			return buf, err
		}
		// protojson never writes a raw newline; compact it anyway so a line is one message.
		var compact bytes.Buffer
		if err := json.Compact(&compact, encoded); err != nil {
			return buf, err
		}
		buf = append(buf, compact.Bytes()...)
		return append(buf, '\n'), nil
	default:
		size := proto.Size(message)
		buf = protowire.AppendVarint(buf, uint64(size))
		return proto.MarshalOptions{Deterministic: true}.MarshalAppend(buf, message)
	}
}

// Writer writes one file. Close flushes it; the caller owns the underlying writer.
type Writer struct {
	out    *bufio.Writer
	format Format
	buf    []byte
}

// NewWriter writes the header of a file of this kind and returns a writer for its messages.
func NewWriter(out io.Writer, format Format, header *pb.FileHeader) (*Writer, error) {
	w := &Writer{out: bufio.NewWriterSize(out, 1<<16), format: format}
	if err := w.Write(header); err != nil {
		return nil, err
	}
	return w, nil
}

// Write appends one message.
func (w *Writer) Write(message proto.Message) error {
	var err error
	w.buf, err = Append(w.buf[:0], w.format, message)
	if err != nil {
		return fmt.Errorf("encode %s: %w", message.ProtoReflect().Descriptor().Name(), err)
	}
	_, err = w.out.Write(w.buf)
	return err
}

// WriteRaw appends messages already encoded with Append in this writer's format.
func (w *Writer) WriteRaw(encoded []byte) error {
	_, err := w.out.Write(encoded)
	return err
}

// Flush writes buffered data to the underlying writer.
func (w *Writer) Flush() error { return w.out.Flush() }

// Reader reads one file's messages after its header.
type Reader struct {
	in     *bufio.Reader
	format Format
	line   int
}

// NewReader reads and checks the header: a supported schema version and the expected kind.
func NewReader(in io.Reader, format Format, kind pb.FileKind) (*Reader, *pb.FileHeader, error) {
	r := &Reader{in: bufio.NewReaderSize(in, 1<<16), format: format}
	header := &pb.FileHeader{}
	if err := r.Read(header); err != nil {
		if errors.Is(err, io.EOF) {
			return nil, nil, fmt.Errorf("%w: empty file, expected a FileHeader", ErrMalformed)
		}
		return nil, nil, fmt.Errorf("FileHeader: %w", err)
	}
	if err := checkHeader(header, kind); err != nil {
		return nil, nil, err
	}
	return r, header, nil
}

func checkHeader(header *pb.FileHeader, kind pb.FileKind) error {
	if header.GetSchemaVersion() != SchemaVersion {
		return fmt.Errorf("%w %d (this loadgen implements %d)", ErrSchemaVersion, header.GetSchemaVersion(), SchemaVersion)
	}
	if header.GetKind() != kind {
		return fmt.Errorf("%w: FileHeader.kind is %s, expected %s", ErrMalformed, header.GetKind(), kind)
	}
	return nil
}

// Frames splits a whole file held in memory into its messages, as Reader reads them, without
// decoding them: a reader can keep the bytes and decode a message when it needs it.
type Frames struct {
	data   []byte
	pos    int
	format Format
	count  int
}

// NewFrames reads and checks the header of a file's bytes, as NewReader does, and returns the
// frames of the messages after it.
func NewFrames(data []byte, format Format, kind pb.FileKind) (*Frames, *pb.FileHeader, error) {
	f := &Frames{data: data, format: format}
	start, end, err := f.Next()
	if err != nil {
		if errors.Is(err, io.EOF) {
			return nil, nil, fmt.Errorf("%w: empty file, expected a FileHeader", ErrMalformed)
		}
		return nil, nil, fmt.Errorf("FileHeader: %w", err)
	}
	header := &pb.FileHeader{}
	if err := Unmarshal(f.Message(start, end), format, header); err != nil {
		return nil, nil, fmt.Errorf("FileHeader: %w", err)
	}
	if err := checkHeader(header, kind); err != nil {
		return nil, nil, err
	}
	return f, header, nil
}

// Next returns where the next message's frame starts and ends in the file (binpb: its length
// prefix included; json: its line, newline included); io.EOF at a clean end of file.
func (f *Frames) Next() (int, int, error) {
	f.count++
	start := f.pos
	if f.format == JSON {
		if start >= len(f.data) || len(bytes.TrimSpace(f.data[start:])) == 0 {
			return 0, 0, io.EOF
		}
		end := len(f.data)
		if newline := bytes.IndexByte(f.data[start:], '\n'); newline >= 0 {
			end = start + newline + 1
		}
		if len(bytes.TrimSpace(f.data[start:end])) == 0 {
			return 0, 0, fmt.Errorf("%w: empty line %d", ErrMalformed, f.count)
		}
		f.pos = end
		return start, end, nil
	}
	if start >= len(f.data) {
		return 0, 0, io.EOF
	}
	size, n := protowire.ConsumeVarint(f.data[start:])
	if n < 0 || size > maxMessageBytes || uint64(len(f.data)-start-n) < size {
		return 0, 0, fmt.Errorf("%w: message %d: truncated or oversized at byte %d", ErrMalformed, f.count, start)
	}
	f.pos = start + n + int(size)
	return start, f.pos, nil
}

// Message returns the encoded message of the frame [start, end) that Next returned.
func (f *Frames) Message(start, end int) []byte { return MessageOf(f.data[start:end], f.format) }

// MessageOf returns the encoded message of one frame (as Next delimits it).
func MessageOf(frame []byte, format Format) []byte {
	if format == JSON {
		return frame
	}
	_, n := protowire.ConsumeVarint(frame)
	return frame[n:]
}

// Unmarshal decodes one encoded message (a frame's, from MessageOf).
func Unmarshal(encoded []byte, format Format, message proto.Message) error {
	var err error
	if format == JSON {
		err = protojson.Unmarshal(encoded, message)
	} else {
		err = proto.Unmarshal(encoded, message)
	}
	if err != nil {
		return fmt.Errorf("%w: %v", ErrMalformed, err)
	}
	return nil
}

// Read decodes the next message into message; io.EOF at a clean end of file.
func (r *Reader) Read(message proto.Message) error {
	r.line++
	switch r.format {
	case JSON:
		for {
			line, err := r.in.ReadBytes('\n')
			if len(bytes.TrimSpace(line)) == 0 {
				if err != nil {
					if errors.Is(err, io.EOF) {
						return io.EOF
					}
					return err
				}
				return fmt.Errorf("%w: empty line %d", ErrMalformed, r.line)
			}
			if err != nil && !errors.Is(err, io.EOF) {
				return err
			}
			if uerr := protojson.Unmarshal(line, message); uerr != nil {
				return fmt.Errorf("%w: message %d: %v", ErrMalformed, r.line, uerr)
			}
			return nil
		}
	default:
		err := protodelim.UnmarshalOptions{MaxSize: maxMessageBytes}.UnmarshalFrom(r.in, message)
		if err == nil {
			return nil
		}
		if errors.Is(err, io.EOF) && !errors.Is(err, io.ErrUnexpectedEOF) {
			return io.EOF
		}
		return fmt.Errorf("%w: message %d: %v", ErrMalformed, r.line, err)
	}
}
