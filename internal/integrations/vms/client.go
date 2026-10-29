// Package vms contiene el adaptador de servidor para /v2. El navegador nunca recibe esta credencial.
package vms

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"kinich-go/internal/domain"
)

type Client struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
}

func (c Client) Do(ctx context.Context, method, path, key string, revision int64, in any, out any) error {
	b, err := json.Marshal(in)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(c.BaseURL, "/")+path, bytes.NewReader(b))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	req.Header.Set("Content-Type", "application/json")
	if key != "" {
		req.Header.Set("Idempotency-Key", key)
	}
	if revision > 0 {
		req.Header.Set("If-Match", fmt.Sprint(revision))
	}
	client := c.HTTP
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	}
	res, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("motor no disponible")
	}
	defer res.Body.Close()
	if res.StatusCode >= 300 {
		return fmt.Errorf("motor rechazó solicitud (%d)", res.StatusCode)
	}
	if out != nil {
		return json.NewDecoder(io.LimitReader(res.Body, 1024*1024)).Decode(out)
	}
	return nil
}

type Alert struct {
	Score            float64            `json:"score"`
	Owner            domain.Owner       `json:"owner"`
	CameraUID        string             `json:"camera_uid"`
	EventID          string             `json:"event_id"`
	ExecutionID      string             `json:"execution_id"`
	MemberID         string             `json:"member_id"`
	Model            string             `json:"model"`
	Type             string             `json:"type"`
	ClosesPhenomenon bool               `json:"closes_phenomenon"`
	SemanticState    string             `json:"semantic_state"`
	Frame            domain.Frame       `json:"frame"`
	Detections       []domain.Detection `json:"detections"`
	EvidenceState    string             `json:"evidence_state"`
}

// ToAlert exige resolver el UUID contra el dueño autenticado antes de crear alerta o medios.
func ToAlert(authenticatedOwner domain.Owner, event domain.Event, resolve func(domain.Owner, string) (string, error)) (Alert, error) {
	if event.Owner != authenticatedOwner {
		return Alert{}, domain.NotFound
	}
	uid, err := resolve(authenticatedOwner, event.CameraID)
	if err != nil || uid == "" {
		return Alert{}, domain.NotFound
	}
	return Alert{Score: event.Score, Owner: authenticatedOwner, CameraUID: uid, EventID: event.ID, ExecutionID: event.ExecutionID, MemberID: event.MemberID, Model: event.Model, Type: event.Type, ClosesPhenomenon: strings.HasSuffix(event.Type, "_OFF"), SemanticState: event.State, Frame: event.Frame, Detections: event.Detections, EvidenceState: "pending"}, nil
}
