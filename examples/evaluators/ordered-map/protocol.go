package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"syscall"
)

const (
	protocolFDBase = 3
	frameSize      = 16
	maxMapSize     = 1 << 20
	maxLaneCount   = 256
)

type scenario uint32

const (
	scenarioSWMR scenario = iota + 1
	scenarioMW
)

func parseScenario(value string) (scenario, error) {
	switch value {
	case "swmr":
		return scenarioSWMR, nil
	case "mw":
		return scenarioMW, nil
	default:
		return 0, fmt.Errorf("unsupported scenario %q", value)
	}
}

func (s scenario) String() string {
	switch s {
	case scenarioSWMR:
		return "swmr"
	case scenarioMW:
		return "mw"
	default:
		return fmt.Sprintf("scenario(%d)", s)
	}
}

type operation uint32

const (
	operationPut operation = iota + 1
	operationGet
	operationRemove
	operationMin
	operationMax
	operationPredecessor
	operationSuccessor
	operationRange
)

type responseStatus uint32

const (
	statusOK responseStatus = iota + 1
	statusMissing
	statusError
)

type request struct {
	operation operation
	key       []byte
	value     []byte
	extra     uint32
}

type rangeItem struct {
	key   []byte
	value []byte
}

type response struct {
	status    responseStatus
	key       []byte
	value     []byte
	extra     uint32
	items     []rangeItem
	remaining bool
}

func createSocketPair(name string) (net.Conn, *os.File, error) {
	descriptors, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_STREAM, 0)
	if err != nil {
		return nil, nil, fmt.Errorf("create socketpair for %s: %w", name, err)
	}
	syscall.CloseOnExec(descriptors[0])
	syscall.CloseOnExec(descriptors[1])

	trustedFile := os.NewFile(uintptr(descriptors[0]), name+"-trusted")
	runnerFile := os.NewFile(uintptr(descriptors[1]), name+"-runner")
	trustedConn, err := net.FileConn(trustedFile)
	if err != nil {
		_ = trustedFile.Close()
		_ = runnerFile.Close()
		return nil, nil, fmt.Errorf("open trusted socket for %s: %w", name, err)
	}
	if err := trustedFile.Close(); err != nil {
		_ = trustedConn.Close()
		_ = runnerFile.Close()
		return nil, nil, fmt.Errorf("close duplicated trusted socket for %s: %w", name, err)
	}
	return trustedConn, runnerFile, nil
}

func writeRequest(writer io.Writer, req request) error {
	data := make([]byte, frameSize+len(req.key)+len(req.value))
	binary.LittleEndian.PutUint32(data[:4], uint32(req.operation))
	binary.LittleEndian.PutUint32(data[4:8], uint32(len(req.key)))
	binary.LittleEndian.PutUint32(data[8:12], uint32(len(req.value)))
	binary.LittleEndian.PutUint32(data[12:16], req.extra)
	copy(data[frameSize:], req.key)
	copy(data[frameSize+len(req.key):], req.value)
	return writeAll(writer, data)
}

func readResponse(reader io.Reader, req request, maxKeySize, maxValueSize int) (response, error) {
	var header [frameSize]byte
	if _, err := io.ReadFull(reader, header[:]); err != nil {
		return response{}, err
	}
	status := responseStatus(binary.LittleEndian.Uint32(header[:4]))
	keyLen := int(binary.LittleEndian.Uint32(header[4:8]))
	valueLen := int(binary.LittleEndian.Uint32(header[8:12]))
	extra := binary.LittleEndian.Uint32(header[12:16])
	if status < statusOK || status > statusError {
		return response{}, fmt.Errorf("unknown response status %d", status)
	}
	if req.operation != operationRange && keyLen > maxKeySize {
		return response{}, fmt.Errorf("response key length %d exceeds maximum %d", keyLen, maxKeySize)
	}
	if req.operation != operationRange && valueLen > maxValueSize {
		return response{}, fmt.Errorf(
			"response value length %d exceeds maximum %d",
			valueLen,
			maxValueSize,
		)
	}
	key := make([]byte, keyLen)
	if _, err := io.ReadFull(reader, key); err != nil {
		return response{}, fmt.Errorf("read response key: %w", err)
	}
	value := make([]byte, valueLen)
	if _, err := io.ReadFull(reader, value); err != nil {
		return response{}, fmt.Errorf("read response value: %w", err)
	}
	resp := response{status: status, key: key, value: value, extra: extra}
	if req.operation == operationRange && status == statusOK {
		items, remaining, err := decodeRangePayload(value, extra, maxKeySize, maxValueSize)
		if err != nil {
			return response{}, err
		}
		resp.items = items
		resp.remaining = remaining
		resp.value = nil
	}
	return resp, nil
}

func decodeRangePayload(payload []byte, extra uint32, maxKeySize, maxValueSize int) ([]rangeItem, bool, error) {
	if extra > 1 {
		return nil, false, fmt.Errorf("range remaining flag is %d", extra)
	}
	if len(payload) < 8 {
		return nil, false, fmt.Errorf("range payload is %d bytes, want at least 8", len(payload))
	}
	count := binary.LittleEndian.Uint64(payload[:8])
	rest := payload[8:]
	items := make([]rangeItem, 0, count)
	for index := uint64(0); index < count; index++ {
		if len(rest) < 16 {
			return nil, false, errors.New("truncated range item header")
		}
		keyLen := int(binary.LittleEndian.Uint64(rest[:8]))
		valueLen := int(binary.LittleEndian.Uint64(rest[8:16]))
		rest = rest[16:]
		if keyLen > maxKeySize || valueLen > maxValueSize {
			return nil, false, fmt.Errorf("range item exceeds configured maxima")
		}
		if len(rest) < keyLen+valueLen {
			return nil, false, errors.New("truncated range item payload")
		}
		item := rangeItem{
			key:   append([]byte(nil), rest[:keyLen]...),
			value: append([]byte(nil), rest[keyLen:keyLen+valueLen]...),
		}
		rest = rest[keyLen+valueLen:]
		items = append(items, item)
	}
	if len(rest) != 0 {
		return nil, false, fmt.Errorf("range payload had %d trailing bytes", len(rest))
	}
	return items, extra == 1, nil
}

func writeAll(writer io.Writer, data []byte) error {
	for len(data) > 0 {
		written, err := writer.Write(data)
		if err != nil {
			return err
		}
		if written == 0 {
			return io.ErrShortWrite
		}
		data = data[written:]
	}
	return nil
}

func closeAll[T interface{ Close() error }](values []T) error {
	var result error
	for _, value := range values {
		result = errors.Join(result, value.Close())
	}
	return result
}
