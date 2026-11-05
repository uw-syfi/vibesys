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
	if header.GetSchemaVersion() != SchemaVersion {
		return nil, nil, fmt.Errorf("%w %d (this loadgen implements %d)", ErrSchemaVersion, header.GetSchemaVersion(), SchemaVersion)
	}
	if header.GetKind() != kind {
		return nil, nil, fmt.Errorf("%w: FileHeader.kind is %s, expected %s", ErrMalformed, header.GetKind(), kind)
	}
	return r, header, nil
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
