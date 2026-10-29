package supervisor

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"kinich-go/internal/domain"
)

const MaxMessage = 1024 * 1024
const LeaseMS = 6000

type Message struct {
	MemberID         string          `json:"member_id,omitempty"`
	MemberGeneration string          `json:"member_generation,omitempty"`
	PermitID         string          `json:"permit_id,omitempty"`
	V                int             `json:"v"`
	Kind             string          `json:"kind"`
	WorkerID         string          `json:"worker_id"`
	ExecutionID      string          `json:"execution_id"`
	Generation       string          `json:"generation"`
	CommandID        string          `json:"command_id,omitempty"`
	Revision         int64           `json:"revision,omitempty"`
	CameraID         string          `json:"camera_id,omitempty"`
	LeaseMS          int             `json:"lease_ms,omitempty"`
	Members          []domain.Member `json:"members,omitempty"`
	Source           string          `json:"source,omitempty"`
	Media            string          `json:"media,omitempty"`
	Mode             string          `json:"mode,omitempty"`
	Event            *domain.Event   `json:"event,omitempty"`
	EventID          string          `json:"event_id,omitempty"`
	Accepted         bool            `json:"accepted,omitempty"`
	Reason           string          `json:"reason,omitempty"`
	State            string          `json:"state,omitempty"`
}
type Process struct {
	leaseUntil time.Time
	Workspace  string
	ID         string
	Execution  domain.Execution
	Cmd        *exec.Cmd
	input      io.WriteCloser
	mu         sync.Mutex
	Done       chan struct{}
	hello      chan struct{}
	Messages   chan Message
	waitErr    error
	stopOnce   sync.Once
}
type Manager struct {
	StateDir     string
	Python       string
	WorkerDir    string
	ControllerID string
	mu           sync.Mutex
	processes    map[string]*Process
}

func New(python, dir string) *Manager {
	return &Manager{Python: python, WorkerDir: dir, ControllerID: domain.ID(), processes: map[string]*Process{}}
}
func (m *Manager) Get(id string) *Process { m.mu.Lock(); defer m.mu.Unlock(); return m.processes[id] }
func (m *Manager) Start(ctx context.Context, e domain.Execution) (*Process, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.processes[e.ID]; ok {
		return nil, errors.New("worker ya existente")
	}
	p := &Process{ID: e.WorkerID, Execution: e, Done: make(chan struct{}), hello: make(chan struct{}), Messages: make(chan Message, 64)}
	cmd := exec.Command(m.Python, "-m", "kinich_worker.runtime")
	root := m.StateDir
	if root == "" {
		root = filepath.Join(os.TempDir(), "kinich-runtime")
	}
	workspace := filepath.Join(root, e.WorkerID)
	if err := os.MkdirAll(workspace, 0700); err != nil {
		return nil, err
	}
	p.Workspace = workspace
	sum := sha256.Sum256([]byte(e.Owner.Key()))
	modelState := filepath.Join(root, "model-state", hex.EncodeToString(sum[:]), e.CameraID)
	if err := os.MkdirAll(modelState, 0700); err != nil {
		return nil, err
	}
	cmd.Dir = workspace
	cmd.Env = append(workerEnvironment(), "KINICH_COUNT_COUNTERS="+modelState, "PYTHONPATH="+m.WorkerDir+":"+m.WorkerDir+"/vendor", "PYTHONUNBUFFERED=1", "OPENCV_IO_MAX_IMAGE_PIXELS=16777216", "KINICH_WORKER_ID="+e.WorkerID, "KINICH_EXECUTION_ID="+e.ID, "KINICH_CAMERA_ID="+e.CameraID, "KINICH_GENERATION="+e.Generation)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Stderr = io.Discard
	input, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	output, err := cmd.StdoutPipe()
	if err != nil {
		input.Close()
		return nil, err
	}
	p.input = input
	p.Cmd = cmd
	if err = cmd.Start(); err != nil {
		input.Close()
		output.Close()
		return nil, err
	}
	m.processes[e.ID] = p
	go func() { _ = cmd.Wait(); p.input.Close(); close(p.Done) }()
	go func() {
		defer close(p.Messages)
		scanner := bufio.NewScanner(output)
		scanner.Buffer(make([]byte, 4096), MaxMessage)
		hello := false
		for scanner.Scan() {
			var msg Message
			dec := json.NewDecoder(bytes.NewReader(scanner.Bytes()))
			dec.DisallowUnknownFields()
			if err := dec.Decode(&msg); err != nil || msg.V != 1 || msg.WorkerID != p.ID || msg.ExecutionID != e.ID || msg.Generation != e.Generation {
				p.Kill()
				return
			}
			if msg.Kind == "hello" {
				if hello {
					p.Kill()
					return
				}
				hello = true
				close(p.hello)
				continue
			}
			if !hello {
				p.Kill()
				return
			}
			select {
			case p.Messages <- msg:
			case <-p.Done:
				return
			}
		}
		if scanner.Err() != nil {
			p.Kill()
		}
	}()
	select {
	case <-p.hello:
		return p, nil
	case <-p.Done:
		return nil, fmt.Errorf("worker terminó antes de handshake")
	case <-ctx.Done():
		p.Kill()
		return nil, ctx.Err()
	case <-time.After(3 * time.Second):
		p.Kill()
		return nil, fmt.Errorf("handshake vencido")
	}
}

func (p *Process) Send(msg Message) error {
	msg.V = 1
	msg.WorkerID = p.ID
	msg.ExecutionID = p.Execution.ID
	msg.Generation = p.Execution.Generation
	if msg.CommandID == "" {
		msg.CommandID = domain.ID()
	}
	b, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	if len(b)+1 > MaxMessage {
		return errors.New("mensaje demasiado grande")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if f, ok := p.input.(*os.File); ok {
		_ = f.SetWriteDeadline(time.Now().Add(time.Second))
	}
	_, err = p.input.Write(append(b, '\n'))
	if err == nil && (msg.Kind == "open" || msg.Kind == "renew") {
		p.leaseUntil = time.Now().Add(time.Duration(msg.LeaseMS) * time.Millisecond)
	}
	return err
}
func (p *Process) Kill() {
	if p.Cmd != nil && p.Cmd.Process != nil {
		_ = syscall.Kill(-p.Cmd.Process.Pid, syscall.SIGKILL)
	}
}
func (p *Process) Stop() {
	p.stopOnce.Do(func() {
		go func() {
			_ = p.Send(Message{Kind: "stop"})
			select {
			case <-p.Done:
			case <-time.After(2 * time.Second):
				_ = syscall.Kill(-p.Cmd.Process.Pid, syscall.SIGTERM)
				select {
				case <-p.Done:
				case <-time.After(time.Second):
					p.Kill()
				}
			}
		}()
	})
}
func (p *Process) Exited() bool {
	select {
	case <-p.Done:
		return GroupGone(p.Cmd.Process.Pid)
	default:
		return false
	}
}
func GroupGone(pid int) bool { return pid > 0 && errors.Is(syscall.Kill(-pid, 0), syscall.ESRCH) }
func (m *Manager) Remove(id string) {
	m.mu.Lock()
	if p := m.processes[id]; p != nil && p.Exited() {
		_ = os.RemoveAll(p.Workspace)
	}
	delete(m.processes, id)
	m.mu.Unlock()
}
func (m *Manager) Shutdown(ctx context.Context) {
	m.mu.Lock()
	ps := make([]*Process, 0, len(m.processes))
	for _, p := range m.processes {
		ps = append(ps, p)
	}
	m.mu.Unlock()
	for _, p := range ps {
		p.Stop()
	}
	for _, p := range ps {
		select {
		case <-p.Done:
		case <-ctx.Done():
			p.Kill()
		}
	}
}

func workerEnvironment() []string {
	out := []string{}
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		allowed := name == "PATH" || name == "HOME" || name == "LANG" || name == "VIRTUAL_ENV" || strings.HasPrefix(name, "CUDA") || strings.HasPrefix(name, "NVIDIA") || strings.HasPrefix(name, "KINICH_") || strings.HasPrefix(name, "FULLCV_") || strings.HasPrefix(name, "COMFY") || strings.HasPrefix(name, "OMP_") || strings.HasPrefix(name, "MKL_")
		secret := strings.Contains(name, "TOKEN") || strings.Contains(name, "SECRET") || strings.Contains(name, "PASSWORD") || strings.Contains(name, "API_KEY")
		if allowed && !secret {
			out = append(out, entry)
		}
	}
	return out
}

func (p *Process) LeaseValid() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return time.Now().Before(p.leaseUntil)
}
