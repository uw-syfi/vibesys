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
	protocolVersion = uint32(1)
	protocolFDBase  = 3
	frameSize       = 16
	maxLaneCount    = 256
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
	candidateFile := os.NewFile(uintptr(descriptors[1]), name+"-candidate")
	trustedConn, err := net.FileConn(trustedFile)
	if err != nil {
		_ = trustedFile.Close()
		_ = candidateFile.Close()
		return nil, nil, fmt.Errorf("open trusted socket for %s: %w", name, err)
	}
	if err := trustedFile.Close(); err != nil {
		_ = trustedConn.Close()
		_ = candidateFile.Close()
		return nil, nil, fmt.Errorf("close duplicated trusted socket for %s: %w", name, err)
	}
	return trustedConn, candidateFile, nil
}

func inheritedSocket(fd int, name string) (net.Conn, error) {
	file := os.NewFile(uintptr(fd), name)
	if file == nil {
		return nil, fmt.Errorf("open inherited socket fd %d", fd)
	}
	conn, err := net.FileConn(file)
	closeErr := file.Close()
	if err != nil {
		return nil, fmt.Errorf("use inherited socket fd %d: %w", fd, err)
	}
	if closeErr != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("close inherited socket fd %d after duplication: %w", fd, closeErr)
	}
	return conn, nil
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

func writeRequests(writer io.Writer, requests []request) error {
	data := make([]byte, len(requests)*frameSize)
	for index, req := range requests {
		offset := index * frameSize
		binary.LittleEndian.PutUint32(data[offset:], uint32(req.operation))
		binary.LittleEndian.PutUint64(data[offset+8:], req.value)
	}
	return writeAll(writer, data)
}

func writeRequest(writer io.Writer, req request) error {
	var data [frameSize]byte
	binary.LittleEndian.PutUint32(data[:], uint32(req.operation))
	binary.LittleEndian.PutUint64(data[8:], req.value)
	return writeAll(writer, data[:])
}

func readRequest(reader io.Reader) (request, error) {
	var data [frameSize]byte
	if _, err := io.ReadFull(reader, data[:]); err != nil {
		return request{}, err
	}
	if reserved := binary.LittleEndian.Uint32(data[4:]); reserved != 0 {
		return request{}, fmt.Errorf("request reserved field is %d, want zero", reserved)
	}
	req := request{
		operation: operation(binary.LittleEndian.Uint32(data[:])),
		value:     binary.LittleEndian.Uint64(data[8:]),
	}
	if req.operation != operationEnqueue && req.operation != operationDequeue {
		return request{}, fmt.Errorf("unknown request operation %d", req.operation)
	}
	return req, nil
}

func writeResponse(writer io.Writer, resp response) error {
	var data [frameSize]byte
	binary.LittleEndian.PutUint32(data[:], uint32(resp.status))
	binary.LittleEndian.PutUint64(data[8:], resp.value)
	return writeAll(writer, data[:])
}

func readResponse(reader io.Reader) (response, error) {
	var data [frameSize]byte
	if _, err := io.ReadFull(reader, data[:]); err != nil {
		return response{}, err
	}
	if reserved := binary.LittleEndian.Uint32(data[4:]); reserved != 0 {
		return response{}, fmt.Errorf("response reserved field is %d, want zero", reserved)
	}
	resp := response{
		status: responseStatus(binary.LittleEndian.Uint32(data[:])),
		value:  binary.LittleEndian.Uint64(data[8:]),
	}
	if resp.status < statusEnqueued || resp.status > statusError {
		return response{}, fmt.Errorf("unknown response status %d", resp.status)
	}
	return resp, nil
}

func closeAll[T interface{ Close() error }](values []T) error {
	var result error
	for _, value := range values {
		result = errors.Join(result, value.Close())
	}
	return result
}
