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
	scenarioSPSC scenario = iota + 1
	scenarioMPSC
	scenarioMPMC
)

func parseScenario(value string) (scenario, error) {
	switch value {
	case "spsc":
		return scenarioSPSC, nil
	case "mpsc":
		return scenarioMPSC, nil
	case "mpmc":
		return scenarioMPMC, nil
	default:
		return 0, fmt.Errorf("unsupported scenario %q", value)
	}
}

func (s scenario) String() string {
	switch s {
	case scenarioSPSC:
		return "spsc"
	case scenarioMPSC:
		return "mpsc"
	case scenarioMPMC:
		return "mpmc"
	default:
		return fmt.Sprintf("scenario(%d)", s)
	}
}

type operation uint32

const (
	operationEnqueue operation = iota + 1
	operationDequeue
)

type responseStatus uint32

const (
	statusEnqueued responseStatus = iota + 1
	statusFull
	statusValue
	statusEmpty
	statusError
)

type request struct {
	operation operation
	value     uint64
}

type response struct {
	status responseStatus
	value  uint64
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

func writeRequest(writer io.Writer, req request, valueSize int) error {
	var payload []byte
	if req.operation == operationEnqueue {
		payload = queuePayload(req.value, valueSize)
	}
	data := make([]byte, frameSize+len(payload))
	binary.LittleEndian.PutUint32(data[:4], uint32(req.operation))
	binary.LittleEndian.PutUint32(data[4:8], uint32(len(payload)))
	copy(data[frameSize:], payload)
	return writeAll(writer, data)
}

func readResponse(reader io.Reader, valueSize int) (response, error) {
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
	if status < statusEnqueued || status > statusError {
		return response{}, fmt.Errorf("unknown response status %d", status)
	}
	if length > valueSize {
		return response{}, fmt.Errorf(
			"response payload length %d exceeds configured value size %d",
			length,
			valueSize,
		)
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(reader, payload); err != nil {
		return response{}, fmt.Errorf("read response payload: %w", err)
	}
	if status == statusValue {
		if length != valueSize {
			return response{}, fmt.Errorf(
				"dequeue returned %d bytes, expected %d",
				length,
				valueSize,
			)
		}
		value, err := queuePayloadValue(payload)
		if err != nil {
			return response{}, err
		}
		return response{status: status, value: value}, nil
	}
	if length != 0 {
		return response{}, fmt.Errorf("response status %d included an unexpected payload", status)
	}
	return response{status: status}, nil
}

func queuePayload(value uint64, size int) []byte {
	payload := make([]byte, size)
	binary.LittleEndian.PutUint64(payload[:8], value)
	lane := byte(value >> 56)
	for index := 8; index < len(payload); index++ {
		payload[index] = lane*31 + byte(index-8)*17 + 0x5d
	}
	return payload
}

func queuePayloadValue(payload []byte) (uint64, error) {
	if len(payload) < minQueueValueSize {
		return 0, fmt.Errorf("payload is %d bytes, want at least %d", len(payload), minQueueValueSize)
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
