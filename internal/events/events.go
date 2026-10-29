package events

import (
	"context"
	"crypto/hmac"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"time"

	"kinich-go/internal/domain"
	"kinich-go/internal/store"
)

type Service struct{ Store *store.Store }

func (s *Service) Accept(ctx context.Context, binding domain.Execution, event domain.Event) error {
	if math.IsNaN(event.Score) || math.IsInf(event.Score, 0) {
		return domain.Error(422, "event_invalid", "Score no finito")
	}
	if len(event.ID) != 36 || len(event.Type) < 1 || len(event.Type) > 80 || len(event.Detections) > 200 {
		return domain.Error(422, "event_invalid", "Evento fuera de límites")
	}
	f := event.Frame
	if f.Width < 1 || f.Width > 16384 || f.Height < 1 || f.Height > 16384 || f.SourceWidth < 1 || f.SourceHeight < 1 || len(f.Transform) != 6 || len(f.ID) == 0 || len(f.ID) > 120 {
		return domain.Error(422, "geometry_invalid", "Geometría inválida")
	}
	capture, err := time.Parse(time.RFC3339Nano, f.CapturedAt)
	if err != nil || capture.After(time.Now().Add(5*time.Second)) || capture.Before(time.Now().Add(-24*time.Hour)) {
		return domain.Error(422, "clock_invalid", "Reloj inválido")
	}
	for _, v := range append(append([]float64(nil), f.Transform...), f.MediaTime) {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return domain.Error(422, "geometry_invalid", "Transformación inválida")
		}
	}
	for _, d := range event.Detections {
		if len(d.BBox) != 4 || math.IsNaN(d.Score) || math.IsInf(d.Score, 0) || d.Score < 0 || d.Score > 1 {
			return domain.Error(422, "detection_invalid", "Detección inválida")
		}
		for _, v := range d.BBox {
			if math.IsNaN(v) || math.IsInf(v, 0) {
				return domain.Error(422, "detection_invalid", "Detección inválida")
			}
		}
		if d.BBox[2] <= d.BBox[0] || d.BBox[3] <= d.BBox[1] {
			return domain.Error(422, "detection_invalid", "Caja sin área")
		}
	}
	return s.Store.Tx(ctx, func(tx *sql.Tx) error {
		// Duplicados ya confirmados conservan ACK incluso después de retirar la sesión.
		var prior string
		err := tx.QueryRowContext(ctx, "SELECT data FROM events WHERE environment=? AND tenant_id=? AND id=?", binding.Environment, binding.TenantID, event.ID).Scan(&prior)
		if err == nil {
			old, err := store.Decode[domain.Event](prior)
			if err != nil {
				return err
			}
			if old.ExecutionID != binding.ID || old.Generation != binding.Generation || old.MemberID != event.MemberID || old.MemberGeneration != event.MemberGeneration || old.Type != event.Type || old.Score != event.Score || domain.JSON(old.Frame) != domain.JSON(event.Frame) || domain.JSON(old.Detections) != domain.JSON(event.Detections) || domain.JSON(old.Extra) != domain.JSON(event.Extra) || old.State != event.State {
				return domain.Conflict
			}
			return nil
		}
		if err != sql.ErrNoRows {
			return err
		}
		current, err := store.Execution(ctx, tx, binding.Owner, binding.ID)
		if err != nil {
			return err
		}
		if current.Desired != "run" || (current.State != "running" && current.State != "preparing") || current.Generation != binding.Generation || current.WorkerID != binding.WorkerID {
			return domain.Error(409, "generation_retired", "Generación retirada")
		}
		var member *domain.Member
		for i := range current.Members {
			m := &current.Members[i]
			if m.ID == event.MemberID && m.Generation == event.MemberGeneration && m.Model == event.Model {
				member = m
				break
			}
		}
		if member == nil {
			return domain.Error(409, "member_retired", "Miembro retirado")
		}
		// El propietario y las identidades de proceso se toman del vínculo autenticado, nunca del plugin.
		event.Owner = current.Owner
		event.CameraID = current.CameraID
		event.ExecutionID = current.ID
		event.WorkerID = current.WorkerID
		event.Generation = current.Generation
		event.Revision = current.Revision
		event.CreatedAt = domain.Now()
		event.Sequence = 0
		res, err := tx.ExecContext(ctx, "INSERT INTO events(id,environment,tenant_id,execution_id,data) VALUES(?,?,?,?,?)", event.ID, current.Environment, current.TenantID, current.ID, domain.JSON(event))
		if err != nil {
			return err
		}
		seq, err := res.LastInsertId()
		if err != nil {
			return err
		}
		event.Sequence = seq
		if _, err = tx.ExecContext(ctx, "UPDATE events SET data=? WHERE sequence=?", domain.JSON(event), seq); err != nil {
			return err
		}
		var revision int64
		err = tx.QueryRowContext(ctx, "SELECT revision FROM destinations WHERE environment=? AND tenant_id=? ORDER BY revision DESC LIMIT 1", current.Environment, current.TenantID).Scan(&revision)
		if err == sql.ErrNoRows {
			return nil
		}
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, "INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?)", domain.ID(), current.Environment, current.TenantID, event.ID, revision, "pending", 0, domain.Now(), 0)
		return err
	})
}
func (s *Service) List(ctx context.Context, o domain.Owner, execution string, after int64, limit int) ([]domain.Event, error) {
	if limit < 1 || limit > 200 {
		return nil, domain.Error(422, "limit", "limit debe ser de 1 a 200")
	}
	q := "SELECT data FROM events WHERE environment=? AND tenant_id=? AND sequence>?"
	args := []any{o.Environment, o.TenantID, after}
	if execution != "" {
		q += " AND execution_id=?"
		args = append(args, execution)
	}
	q += " ORDER BY sequence LIMIT ?"
	args = append(args, limit)
	rows, err := s.Store.DB.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []domain.Event{}
	for rows.Next() {
		var data string
		if err = rows.Scan(&data); err != nil {
			return nil, err
		}
		v, err := store.Decode[domain.Event](data)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func (s *Service) Event(ctx context.Context, o domain.Owner, id string) (domain.Event, error) {
	var d string
	err := s.Store.DB.QueryRowContext(ctx, "SELECT data FROM events WHERE environment=? AND tenant_id=? AND id=?", o.Environment, o.TenantID, id).Scan(&d)
	if err != nil {
		return domain.Event{}, store.Missing(err)
	}
	return store.Decode[domain.Event](d)
}
func Cursor(s *store.Store, o domain.Owner, execution string, seq int64) string {
	return fmt.Sprintf("%d.%s", seq, s.Sign(o.Key() + "/" + execution + "/" + fmt.Sprint(seq))[:32])
}
func ParseCursor(s *store.Store, o domain.Owner, execution, cursor string) (int64, error) {
	if cursor == "" {
		return 0, nil
	}
	var seq int64
	var sig string
	if _, err := fmt.Sscanf(cursor, "%d.%s", &seq, &sig); err != nil || seq < 0 || !hmac.Equal([]byte(Cursor(s, o, execution, seq)), []byte(cursor)) {
		return 0, domain.Error(400, "invalid_cursor", "Cursor de otro ámbito o inválido")
	}
	return seq, nil
}
func Encode(v any) []byte { b, _ := json.Marshal(v); return b }
