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
)

func (op operation) String() string {
	switch op {
	case operationPut:
		return "put"
	case operationGet:
		return "get"
	case operationRemove:
		return "remove"
	default:
		return fmt.Sprintf("operation(%d)", op)
	}
}

type responseStatus uint32

const (
	statusOK responseStatus = iota + 1
	statusMissing
	statusInvalid
	statusError
)

func (status responseStatus) String() string {
	switch status {
	case statusOK:
		return "ok"
	case statusMissing:
		return "missing"
	case statusInvalid:
		return "invalid"
	case statusError:
		return "error"
	default:
		return fmt.Sprintf("status(%d)", status)
	}
}

type request struct {
	operation operation
	key       []byte
	value     []byte
}

type response struct {
	status responseStatus
	value  []byte
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

func writeRequest(writer io.Writer, req request, maxKeySize, maxValueSize int) error {
	if len(req.key) > maxKeySize {
		return fmt.Errorf("request key length %d exceeds maximum %d", len(req.key), maxKeySize)
	}
	if len(req.value) > maxValueSize {
		return fmt.Errorf("request value length %d exceeds maximum %d", len(req.value), maxValueSize)
	}
	if req.operation != operationPut && len(req.value) != 0 {
		return errors.New("get/remove request contains a value payload")
	}
	data := make([]byte, frameSize+len(req.key)+len(req.value))
	binary.LittleEndian.PutUint32(data[:4], uint32(req.operation))
	binary.LittleEndian.PutUint32(data[4:8], uint32(len(req.key)))
	binary.LittleEndian.PutUint32(data[8:12], uint32(len(req.value)))
	copy(data[frameSize:], req.key)
	copy(data[frameSize+len(req.key):], req.value)
	return writeAll(writer, data)
}

func readResponse(reader io.Reader, maxValueSize int) (response, error) {
	var header [frameSize]byte
	if _, err := io.ReadFull(reader, header[:]); err != nil {
		return response{}, err
	}
	status := responseStatus(binary.LittleEndian.Uint32(header[:4]))
	length := int(binary.LittleEndian.Uint32(header[4:8]))
	reserved := binary.LittleEndian.Uint64(header[8:])
	if reserved != 0 {
		return response{}, fmt.Errorf("response reserved field is %d, want zero", reserved)
	}
	if status < statusOK || status > statusError {
		return response{}, fmt.Errorf("unknown response status %d", status)
	}
	if length > maxValueSize {
		return response{}, fmt.Errorf(
			"response payload length %d exceeds configured value size %d",
			length,
			maxValueSize,
		)
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(reader, payload); err != nil {
		return response{}, fmt.Errorf("read response payload: %w", err)
	}
	if status == statusOK {
		return response{status: status, value: payload}, nil
	}
	if length != 0 {
		return response{}, fmt.Errorf("response status %d included an unexpected payload", status)
	}
	return response{status: status}, nil
}

func mapPayload(value uint64, size int) []byte {
	payload := make([]byte, size)
	binary.LittleEndian.PutUint64(payload[:8], value)
	lane := byte(value >> 56)
	for index := 8; index < len(payload); index++ {
		payload[index] = lane*31 + byte(index-8)*17 + 0x5d
	}
	return payload
}

func mapPayloadValue(payload []byte) (uint64, error) {
	if len(payload) < minMapPayloadSize {
		return 0, fmt.Errorf("payload is %d bytes, want at least %d", len(payload), minMapPayloadSize)
	}
	value := binary.LittleEndian.Uint64(payload[:8])
	lane := byte(value >> 56)
	for index := 8; index < len(payload); index++ {
		expected := lane*31 + byte(index-8)*17 + 0x5d
		if payload[index] != expected {
			return 0, fmt.Errorf(
				"payload byte %d is %d, want %d",
				index,
				payload[index],
				expected,
			)
		}
	}
	return value, nil
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
