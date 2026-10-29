package api

import (
	"database/sql"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"kinich-go/internal/domain"
	"kinich-go/internal/events"
	"kinich-go/internal/store"
)

func (s *Server) listEvents(w http.ResponseWriter, r *http.Request) {
	o := principal(r).Owner
	execution := r.URL.Query().Get("execution_id")
	if execution != "" {
		if _, err := s.Store.Execution(r.Context(), o, execution); err != nil {
			s.fail(w, err)
			return
		}
	}
	after, err := events.ParseCursor(s.Store, o, execution, r.URL.Query().Get("cursor"))
	if err != nil {
		s.fail(w, err)
		return
	}
	if r.URL.Query().Get("cursor") != "" {
		if err = s.Events.CheckCursor(r.Context(), o, after); err != nil {
			s.fail(w, err)
			return
		}
	}
	limit := 100
	if v := r.URL.Query().Get("limit"); v != "" {
		limit, err = strconv.Atoi(v)
		if err != nil {
			s.fail(w, domain.Error(400, "limit", "Límite inválido"))
			return
		}
	}
	es, err := s.Events.List(r.Context(), o, execution, after, limit)
	if err != nil {
		s.fail(w, err)
		return
	}
	if len(es) > 0 {
		after = es[len(es)-1].Sequence
	}
	write(w, 200, map[string]any{"events": es, "cursor": events.Cursor(s.Store, o, execution, after), "retention": "30_days_with_pending_delivery_protection"})
}
func (s *Server) live(w http.ResponseWriter, r *http.Request) {
	p := principal(r)
	id := r.PathValue("id")
	if _, err := s.Store.Execution(r.Context(), p.Owner, id); err != nil {
		s.fail(w, err)
		return
	}
	cursor := r.Header.Get("Last-Event-ID")
	if cursor == "" {
		cursor = r.URL.Query().Get("cursor")
	}
	after, err := events.ParseCursor(s.Store, p.Owner, id, cursor)
	if err != nil {
		s.fail(w, err)
		return
	}
	if cursor != "" {
		if err = s.Events.CheckCursor(r.Context(), p.Owner, after); err != nil {
			s.fail(w, err)
			return
		}
	}
	if s.streams.Add(1) > 64 {
		s.streams.Add(-1)
		s.fail(w, domain.Error(429, "subscriptions_full", "Límite de suscripciones"))
		return
	}
	defer s.streams.Add(-1)
	f, ok := w.(http.Flusher)
	if !ok {
		s.fail(w, domain.Error(500, "stream_unavailable", "SSE no disponible"))
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("X-Accel-Buffering", "no")
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	lastKeepalive := time.Now()
	deadline := time.NewTimer(15 * time.Minute)
	defer deadline.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-deadline.C:
			return
		case <-ticker.C:
			// Revalidar antes de cada bloque para cortar revocación sin otro lote autorizado por caché.
			if _, err := s.Auth.Verify(r.Context(), r.Header.Get("Authorization")[7:]); err != nil {
				return
			}
			es, err := s.Events.List(r.Context(), p.Owner, id, after, 100)
			if err != nil {
				return
			}
			rc := http.NewResponseController(w)
			_ = rc.SetWriteDeadline(time.Now().Add(2 * time.Second))
			for _, e := range es {
				if _, err := s.Auth.Verify(r.Context(), r.Header.Get("Authorization")[7:]); err != nil {
					return
				}
				after = e.Sequence
				if _, err = fmt.Fprintf(w, "id: %s\nevent: detection\ndata: %s\n\n", events.Cursor(s.Store, p.Owner, id, after), domain.JSON(e)); err != nil {
					return
				}
			}
			if time.Since(lastKeepalive) > 10*time.Second {
				if _, err = fmt.Fprint(w, ": heartbeat\n\n"); err != nil {
					return
				}
				lastKeepalive = time.Now()
			}
			f.Flush()
		}
	}
}
func (s *Server) overlay(w http.ResponseWriter, r *http.Request) {
	o := principal(r).Owner
	id := r.PathValue("id")
	e, err := s.Store.Execution(r.Context(), o, id)
	if err != nil {
		s.fail(w, err)
		return
	}
	var d string
	err = s.Store.DB.QueryRowContext(r.Context(), "SELECT data FROM events WHERE environment=? AND tenant_id=? AND execution_id=? ORDER BY sequence DESC LIMIT 1", o.Environment, o.TenantID, id).Scan(&d)
	if err == sql.ErrNoRows || !e.Active() || e.Desired != "run" {
		write(w, 200, map[string]any{"execution_id": id, "available": false})
		return
	}
	if err != nil {
		s.fail(w, err)
		return
	}
	v, err := store.Decode[domain.Event](d)
	if err != nil {
		s.fail(w, err)
		return
	}
	valid := false
	for _, m := range e.Members {
		if m.ID == v.MemberID && m.Generation == v.MemberGeneration {
			valid = true
		}
	}
	capture, _ := time.Parse(time.RFC3339Nano, v.Frame.CapturedAt)
	if !valid || time.Since(capture) > 5*time.Second {
		write(w, 200, map[string]any{"execution_id": id, "available": false})
		return
	}
	write(w, 200, map[string]any{"available": true, "event": v})
}
func (s *Server) capacity(w http.ResponseWriter, r *http.Request) {
	o := principal(r).Owner
	var used, count int
	if err := s.Store.DB.QueryRowContext(r.Context(), "SELECT COALESCE(SUM(memory_mib),0),COUNT(*) FROM reservations WHERE environment=? AND tenant_id=?", o.Environment, o.TenantID).Scan(&used, &count); err != nil {
		s.fail(w, err)
		return
	}
	t, err := s.Store.Tenant(r.Context(), o)
	if err != nil {
		s.fail(w, err)
		return
	}
	write(w, 200, map[string]any{"quota": t.Quota, "reserved_mib": used, "workers_reserved": count, "hardware_certified": false, "max_batch": 1})
}
func (s *Server) workers(w http.ResponseWriter, r *http.Request) {
	o := principal(r).Owner
	rows, err := s.Store.DB.QueryContext(r.Context(), "SELECT id,execution_id,state FROM workers WHERE environment=? AND tenant_id=? ORDER BY rowid DESC LIMIT 200", o.Environment, o.TenantID)
	if err != nil {
		s.fail(w, err)
		return
	}
	defer rows.Close()
	out := []map[string]string{}
	for rows.Next() {
		var id, e, state string
		if err = rows.Scan(&id, &e, &state); err != nil {
			s.fail(w, err)
			return
		}
		out = append(out, map[string]string{"id": id, "execution_id": e, "state": state})
	}
	write(w, 200, out)
}
func (s *Server) deliveries(w http.ResponseWriter, r *http.Request) {
	v, err := s.Outbox.List(r.Context(), principal(r).Owner)
	if err != nil {
		s.fail(w, err)
		return
	}
	write(w, 200, v)
}
func (s *Server) retryDelivery(w http.ResponseWriter, r *http.Request) {
	s.command(w, r, map[string]string{}, func(tx *sql.Tx, op *domain.Operation) error {
		o := principal(r).Owner
		res, err := tx.Exec("UPDATE deliveries SET state='pending',attempts=0,next_at=? WHERE environment=? AND tenant_id=? AND id=? AND state='dead'", domain.Now(), o.Environment, o.TenantID, r.PathValue("id"))
		if err != nil {
			return err
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			return domain.NotFound
		}
		op.ResourceID = r.PathValue("id")
		return nil
	})
}
func (s *Server) destination(w http.ResponseWriter, r *http.Request) {
	var in struct {
		URL   string `json:"url"`
		Token string `json:"token"`
	}
	if err := read(w, r, &in); err != nil {
		s.fail(w, err)
		return
	}
	if err := events.ValidateCallbackToken(in.Token); err != nil {
		s.fail(w, err)
		return
	}
	if err := s.Outbox.Policy.Validate(in.URL); err != nil {
		s.fail(w, err)
		return
	}
	s.command(w, r, in, func(tx *sql.Tx, op *domain.Operation) error {
		o := principal(r).Owner
		sealed, err := s.Store.Seal(o, in.Token)
		if err != nil {
			return err
		}
		var rev int64
		if err = tx.QueryRow("SELECT COALESCE(MAX(revision),0)+1 FROM destinations WHERE environment=? AND tenant_id=?", o.Environment, o.TenantID).Scan(&rev); err != nil {
			return err
		}
		_, err = tx.Exec("INSERT INTO destinations VALUES(?,?,?,?,?)", o.Environment, o.TenantID, rev, in.URL, sealed)
		op.Result = map[string]int64{"revision": rev}
		return err
	})
}
func (s *Server) audit(w http.ResponseWriter, r *http.Request) {
	o := principal(r).Owner
	rows, err := s.Store.DB.QueryContext(r.Context(), "SELECT actor,action,resource,request_id,result,created_at FROM audit WHERE environment=? AND tenant_id=? ORDER BY sequence DESC LIMIT 200", o.Environment, o.TenantID)
	if err != nil {
		s.fail(w, err)
		return
	}
	defer rows.Close()
	out := []map[string]string{}
	for rows.Next() {
		var actor, action, resource, requestID, result, at string
		if err = rows.Scan(&actor, &action, &resource, &requestID, &result, &at); err != nil {
			s.fail(w, err)
			return
		}
		out = append(out, map[string]string{"actor": actor, "action": action, "resource": resource, "request_id": requestID, "result": result, "created_at": at})
	}
	write(w, 200, out)
}
func (s *Server) metrics(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintf(w, "# TYPE kinich_http_requests_total counter\nkinich_http_requests_total %d\n# TYPE kinich_http_errors_total counter\nkinich_http_errors_total %d\n# TYPE kinich_sse_connections gauge\nkinich_sse_connections %d\n", s.requests.Load(), s.errors.Load(), s.streams.Load())
}
