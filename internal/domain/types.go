package domain

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"
)

type Owner struct {
	Environment string `json:"environment"`
	TenantID    string `json:"tenant_id"`
}

func (o Owner) Key() string { return o.Environment + "/" + o.TenantID }
func ID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	s := hex.EncodeToString(b)
	return fmt.Sprintf("%s-%s-%s-%s-%s", s[:8], s[8:12], s[12:16], s[16:20], s[20:])
}
func Now() string { return time.Now().UTC().Format(time.RFC3339Nano) }

type Fault struct {
	Status  int
	Code    string
	Message string
}

func (e *Fault) Error() string                 { return e.Code + ": " + e.Message }
func Error(status int, code, msg string) error { return &Fault{status, code, msg} }

var NotFound = &Fault{404, "not_found", "Recurso no encontrado"}
var Conflict = &Fault{409, "conflict", "El recurso tiene trabajo activo o la clave cambió de contenido"}
var Stale = &Fault{412, "revision_conflict", "La revisión cambió; vuelve a consultar el recurso"}

type Principal struct {
	Owner
	KeyID  string   `json:"key_id"`
	Scopes []string `json:"scopes"`
}

func (p Principal) Can(scope string) bool {
	for _, s := range p.Scopes {
		if s == scope || s == "*" {
			return true
		}
	}
	return false
}

type Quota struct {
	Cameras    int `json:"cameras"`
	Executions int `json:"executions"`
	MemoryMiB  int `json:"memory_mib"`
	Queue      int `json:"queue"`
}
type Tenant struct {
	Owner
	Quota Quota `json:"quota"`
}
type Member struct {
	ID         string         `json:"member_id"`
	Model      string         `json:"model"`
	Version    string         `json:"version"`
	Config     map[string]any `json:"config"`
	FPS        float64        `json:"fps"`
	Generation string         `json:"generation"`
	State      string         `json:"state"`
	Policy     string         `json:"resident_policy"`
}
type Camera struct {
	Owner
	ID        string   `json:"id"`
	Code      string   `json:"code"`
	Name      string   `json:"name"`
	Enabled   bool     `json:"enabled"`
	Revision  int64    `json:"revision"`
	Members   []Member `json:"members"`
	Source    string   `json:"-"`
	CreatedAt string   `json:"created_at"`
}
type Execution struct {
	ObservedRevision int64 `json:"observed_revision"`
	Owner
	ID          string   `json:"id"`
	CameraID    string   `json:"camera_id"`
	Generation  string   `json:"generation"`
	Revision    int64    `json:"revision"`
	State       string   `json:"state"`
	Desired     string   `json:"desired"`
	Reason      string   `json:"reason,omitempty"`
	Terminal    string   `json:"terminal,omitempty"`
	WorkerID    string   `json:"worker_id,omitempty"`
	Members     []Member `json:"members"`
	Kind        string   `json:"kind"`
	CreatedAt   string   `json:"created_at"`
	Deadline    string   `json:"queue_deadline"`
	FinishedAt  string   `json:"finished_at,omitempty"`
	OperationID string   `json:"operation_id"`
	Media       string   `json:"-"`
}

func (e Execution) Active() bool {
	return e.State == "queued" || e.State == "preparing" || e.State == "running" || e.State == "stopping"
}

type Operation struct {
	Owner
	ID         string `json:"id"`
	RequestID  string `json:"request_id"`
	Kind       string `json:"kind"`
	State      string `json:"state"`
	ResourceID string `json:"resource_id"`
	Result     any    `json:"result,omitempty"`
	CreatedAt  string `json:"created_at"`
}
type Frame struct {
	ID           string    `json:"id"`
	CapturedAt   string    `json:"capturedAt"`
	MediaTime    float64   `json:"media_time"`
	Width        int       `json:"width"`
	Height       int       `json:"height"`
	SourceWidth  int       `json:"source_width"`
	SourceHeight int       `json:"source_height"`
	Transform    []float64 `json:"transform"`
}
type Detection struct {
	Label string    `json:"label"`
	Score float64   `json:"score"`
	BBox  []float64 `json:"bbox"`
}
type Event struct {
	Score float64 `json:"score"`
	Owner
	ID               string         `json:"id"`
	Sequence         int64          `json:"sequence"`
	CameraID         string         `json:"camera_id"`
	ExecutionID      string         `json:"execution_id"`
	WorkerID         string         `json:"worker_id"`
	Generation       string         `json:"generation"`
	MemberID         string         `json:"member_id"`
	MemberGeneration string         `json:"member_generation"`
	Model            string         `json:"model"`
	Revision         int64          `json:"revision"`
	Type             string         `json:"type"`
	State            string         `json:"state,omitempty"`
	Frame            Frame          `json:"frame"`
	Detections       []Detection    `json:"detections"`
	Extra            map[string]any `json:"extra,omitempty"`
	CreatedAt        string         `json:"created_at"`
}
type Schedule struct {
	Owner
	ID       string `json:"id"`
	CameraID string `json:"camera_id"`
	Revision int64  `json:"revision"`
	Timezone string `json:"timezone"`
	Hour     int    `json:"hour"`
	Minute   int    `json:"minute"`
	Weekdays []int  `json:"weekdays"`
	Enabled  bool   `json:"enabled"`
	Overlap  string `json:"overlap"`
}
type Destination struct {
	Owner
	Revision int64  `json:"revision"`
	URL      string `json:"url"`
	Token    string `json:"-"`
}
type Delivery struct {
	ID                  string `json:"id"`
	EventID             string `json:"event_id"`
	State               string `json:"state"`
	Attempts            int    `json:"attempts"`
	NextAt              string `json:"next_at"`
	LastStatus          int    `json:"last_status"`
	DestinationRevision int64  `json:"destination_revision"`
}
type Profile struct {
	ResidentMiB int    `json:"resident_mib"`
	SessionMiB  int    `json:"session_mib"`
	WarmupMiB   int    `json:"warmup_mib"`
	InFlight    int    `json:"in_flight"`
	Measured    bool   `json:"measured"`
	Hardware    string `json:"hardware"`
}
type Model struct {
	Name         string         `json:"name"`
	Version      string         `json:"version"`
	Task         string         `json:"task"`
	Capabilities []string       `json:"capabilities"`
	EventTypes   []string       `json:"event_types"`
	ConfigSchema map[string]any `json:"config_schema"`
	TargetFPS    float64        `json:"target_fps"`
	Runtime      string         `json:"runtime"`
	Available    bool           `json:"available"`
	Reason       string         `json:"reason,omitempty"`
	Profile      *Profile       `json:"profile,omitempty"`
}

func JSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return string(b)
}
