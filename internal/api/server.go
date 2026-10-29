package api

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"kinich-go/internal/auth"
	"kinich-go/internal/catalog"
	"kinich-go/internal/domain"
	"kinich-go/internal/events"
	"kinich-go/internal/reconcile"
	"kinich-go/internal/store"
)

type Server struct {
	Store          *store.Store
	Auth           auth.Auth
	Catalog        *catalog.Catalog
	Reconciler     *reconcile.Reconciler
	Events         *events.Service
	Outbox         *events.Outbox
	AllowSynthetic bool
	requests       atomic.Int64
	errors         atomic.Int64
	streams        atomic.Int64
}
type principalKey struct{}

func principal(r *http.Request) domain.Principal {
	return r.Context().Value(principalKey{}).(domain.Principal)
}
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { write(w, 200, map[string]string{"status": "ok"}) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		if err := s.Store.DB.PingContext(ctx); err != nil {
			write(w, 503, map[string]string{"status": "store_unavailable"})
			return
		}
		write(w, 200, map[string]string{"status": "ready", "capacity": "profile_dependent"})
	})
	mux.Handle("GET /metrics", s.secure("admin", s.metrics))
	mux.Handle("GET /v2/models", s.secure("read", s.models))
	mux.Handle("GET /v2/models/{id}", s.secure("read", s.model))
	mux.Handle("GET /v2/cameras", s.secure("read", s.cameras))
	mux.Handle("POST /v2/cameras", s.secure("control", s.createCamera))
	mux.Handle("GET /v2/cameras/{id}", s.secure("read", s.camera))
	mux.Handle("PATCH /v2/cameras/{id}", s.secure("control", s.patchCamera))
	mux.Handle("PUT /v2/cameras/{id}/composition", s.secure("control", s.composition))
	mux.Handle("POST /v2/cameras/{id}/members", s.secure("control", s.addMember))
	mux.Handle("PATCH /v2/cameras/{id}/members/{member}", s.secure("control", s.patchMember))
	mux.Handle("DELETE /v2/cameras/{id}/members/{member}", s.secure("control", s.deleteMember))
	mux.Handle("POST /v2/cameras/{id}/executions", s.secure("control", s.start))
	mux.Handle("POST /v2/cameras/{id}/disconnect", s.secure("control", s.disconnect))
	mux.Handle("POST /v2/cameras/{id}/reactivate", s.secure("control", s.reactivate))
	mux.Handle("GET /v2/executions/{id}", s.secure("read", s.execution))
	mux.Handle("POST /v2/executions/{id}/stop", s.secure("control", s.stop))
	mux.Handle("GET /v2/operations/{id}", s.secure("read", s.operation))
	mux.Handle("POST /v2/queue/clear", s.secure("control", s.clearQueue))
	mux.Handle("GET /v2/events", s.secure("events", s.listEvents))
	mux.Handle("GET /v2/executions/{id}/events/live", s.secure("events", s.live))
	mux.Handle("GET /v2/executions/{id}/overlay", s.secure("events", s.overlay))
	mux.Handle("GET /v2/capacity", s.secure("read", s.capacity))
	mux.Handle("GET /v2/workers", s.secure("read", s.workers))
	mux.Handle("GET /v2/deliveries", s.secure("events", s.deliveries))
	mux.Handle("POST /v2/deliveries/{id}/retry", s.secure("admin", s.retryDelivery))
	mux.Handle("PUT /v2/destination", s.secure("admin", s.destination))
	mux.Handle("POST /v2/predictions", s.secure("predict", s.prediction))
	mux.Handle("GET /v2/schedules", s.secure("read", s.schedules))
	mux.Handle("POST /v2/schedules", s.secure("control", s.createSchedule))
	mux.Handle("PATCH /v2/schedules/{id}", s.secure("control", s.patchSchedule))
	mux.Handle("DELETE /v2/schedules/{id}", s.secure("control", s.deleteSchedule))
	mux.Handle("POST /v2/credentials", s.secure("admin", s.issueKey))
	mux.Handle("DELETE /v2/credentials/{id}", s.secure("admin", s.revokeKey))
	mux.Handle("GET /v2/audit", s.secure("admin", s.audit))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.requests.Add(1)
		w.Header().Set("X-Request-ID", domain.ID())
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		defer func() {
			if recover() != nil {
				s.fail(w, errors.New("request_panic"))
			}
		}()
		mux.ServeHTTP(w, r)
	})
}
func (s *Server) secure(scope string, next http.HandlerFunc) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := r.Header.Get("Authorization")
		if !strings.HasPrefix(h, "Bearer ") {
			s.fail(w, domain.Error(401, "unauthorized", "Credencial requerida"))
			return
		}
		p, err := s.Auth.Verify(r.Context(), strings.TrimPrefix(h, "Bearer "))
		if err != nil {
			s.fail(w, err)
			return
		}
		if !p.Can(scope) {
			s.fail(w, domain.Error(403, "forbidden", "Scope insuficiente"))
			return
		}
		next(w, r.WithContext(context.WithValue(r.Context(), principalKey{}, p)))
	})
}
func write(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func (s *Server) fail(w http.ResponseWriter, err error) {
	s.errors.Add(1)
	var f *domain.Fault
	if !errors.As(err, &f) {
		f = &domain.Fault{Status: 500, Code: "internal", Message: "Error interno; consulta el request ID"}
	}
	write(w, f.Status, map[string]string{"error": f.Code, "message": f.Message, "request_id": w.Header().Get("X-Request-ID")})
}
func read(w http.ResponseWriter, r *http.Request, v any) error {
	r.Body = http.MaxBytesReader(w, r.Body, 700*1024)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if err := d.Decode(v); err != nil {
		return domain.Error(400, "invalid_json", "JSON inválido, campo desconocido o tamaño excedido")
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return domain.Error(400, "invalid_json", "Se requiere un solo objeto JSON")
	}
	return nil
}
func revision(r *http.Request) (int64, error) {
	h := strings.Trim(r.Header.Get("If-Match"), "\"")
	n, err := strconv.ParseInt(h, 10, 64)
	if err != nil || n < 1 {
		return 0, domain.Error(428, "revision_required", "If-Match con revisión vigente es obligatorio")
	}
	return n, nil
}
func (s *Server) command(w http.ResponseWriter, r *http.Request, input any, fn func(*sql.Tx, *domain.Operation) error) {
	digest := s.Store.Sign(r.Method + " " + r.URL.Path + " " + r.Header.Get("If-Match") + " " + domain.JSON(input))
	op, err := s.Store.Command(r.Context(), principal(r), r.Header.Get("Idempotency-Key"), digest, r.Pattern, w.Header().Get("X-Request-ID"), fn)
	if err != nil {
		s.fail(w, err)
		return
	}
	status := 200
	if op.State == "pending" {
		status = 202
	}
	write(w, status, op)
}
func (s *Server) models(w http.ResponseWriter, r *http.Request) { write(w, 200, s.Catalog.List()) }
func (s *Server) model(w http.ResponseWriter, r *http.Request) {
	m, ok := s.Catalog.Models[r.PathValue("id")]
	if !ok {
		s.fail(w, domain.NotFound)
		return
	}
	write(w, 200, m)
}
func (s *Server) cameras(w http.ResponseWriter, r *http.Request) {
	v, err := s.Store.Cameras(r.Context(), principal(r).Owner)
	if err != nil {
		s.fail(w, err)
		return
	}
	write(w, 200, v)
}
func (s *Server) camera(w http.ResponseWriter, r *http.Request) {
	v, err := s.Store.Camera(r.Context(), principal(r).Owner, r.PathValue("id"))
	if err != nil {
		s.fail(w, err)
		return
	}
	w.Header().Set("ETag", fmt.Sprintf("\"%d\"", v.Revision))
	write(w, 200, v)
}

var codePattern = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,120}$`)

func (s *Server) validateSource(source string) error {
	if len(source) > 4096 {
		return domain.Error(422, "source", "Fuente demasiado larga")
	}
	u, err := url.Parse(source)
	if err != nil || u.Hostname() == "" || (u.Scheme != "rtsp" && u.Scheme != "rtsps" && !(s.AllowSynthetic && u.Scheme == "synthetic")) {
		return domain.Error(422, "source", "Se requiere una fuente RTSP válida")
	}
	return nil
}
func (s *Server) createCamera(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Code    string          `json:"code"`
		Name    string          `json:"name"`
		Source  string          `json:"source"`
		Members []domain.Member `json:"members"`
	}
	if err := read(w, r, &in); err != nil {
		s.fail(w, err)
		return
	}
	if !codePattern.MatchString(in.Code) || len(in.Name) < 1 || len(in.Name) > 200 {
		s.fail(w, domain.Error(422, "camera", "Código o nombre inválido"))
		return
	}
	if err := s.validateSource(in.Source); err != nil {
		s.fail(w, err)
		return
	}
	members := []domain.Member{}
	var err error
	if len(in.Members) > 0 {
		members, err = s.Catalog.Validate(in.Members, "stream")
		if err != nil {
			s.fail(w, err)
			return
		}
	}
	s.command(w, r, in, func(tx *sql.Tx, op *domain.Operation) error {
		o := principal(r).Owner
		var count int
		var q string
		if err := tx.QueryRow("SELECT COUNT(*) FROM cameras WHERE environment=? AND tenant_id=?", o.Environment, o.TenantID).Scan(&count); err != nil {
			return err
		}
		if err := tx.QueryRow("SELECT quota FROM tenants WHERE environment=? AND tenant_id=?", o.Environment, o.TenantID).Scan(&q); err != nil {
			return err
		}
		quota, err := store.Decode[domain.Quota](q)
		if err != nil {
			return err
		}
		if count >= quota.Cameras {
			return domain.Error(409, "camera_quota", "Cupo de cámaras agotado")
		}
		c := domain.Camera{Owner: o, ID: domain.ID(), Code: in.Code, Name: in.Name, Enabled: true, Revision: 1, Members: members, CreatedAt: domain.Now()}
		sealed, err := s.Store.Seal(o, in.Source)
		if err != nil {
			return err
		}
		if _, err = tx.Exec("INSERT INTO cameras VALUES(?,?,?,?,?,?,?,?)", c.ID, c.Environment, c.TenantID, c.Code, c.Enabled, c.Revision, sealed, domain.JSON(c)); err != nil {
			return store.Conflict(err)
		}
		op.ResourceID = c.ID
		op.Result = c
		return nil
	})
}
func (s *Server) patchCamera(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Name   *string `json:"name"`
		Source *string `json:"source"`
	}
	if err := read(w, r, &in); err != nil {
		s.fail(w, err)
		return
	}
	rev, err := revision(r)
	if err != nil {
		s.fail(w, err)
		return
	}
	if in.Name != nil && (len(*in.Name) < 1 || len(*in.Name) > 200) {
		s.fail(w, domain.Error(422, "camera", "Nombre inválido"))
		return
	}
	if in.Source != nil {
		if err = s.validateSource(*in.Source); err != nil {
			s.fail(w, err)
			return
		}
	}
	s.command(w, r, in, func(tx *sql.Tx, op *domain.Operation) error {
		c, err := store.Camera(r.Context(), tx, principal(r).Owner, r.PathValue("id"))
		if err != nil {
			return err
		}
		if c.Revision != rev {
			return domain.Stale
		}
		if in.Name != nil {
			c.Name = *in.Name
		}
		if in.Source != nil {
			sealed, err := s.Store.Seal(c.Owner, *in.Source)
			if err != nil {
				return err
			}
			if _, err = tx.Exec("UPDATE cameras SET source=? WHERE environment=? AND tenant_id=? AND id=?", sealed, c.Environment, c.TenantID, c.ID); err != nil {
				return err
			}
			if err = retireCamera(tx, c.Owner, c.ID, "source_rotated"); err != nil {
				return err
			}
			op.State = "pending"
		}
		c.Revision++
		if err = store.SaveCamera(tx, c); err != nil {
			return err
		}
		op.ResourceID = c.ID
		op.Result = c
		return nil
	})
}
func activeCamera(tx *sql.Tx, o domain.Owner, id string) (*domain.Execution, error) {
	var d string
	err := tx.QueryRow("SELECT data FROM executions WHERE environment=? AND tenant_id=? AND camera_id=? AND state IN ('queued','preparing','running','stopping')", o.Environment, o.TenantID, id).Scan(&d)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	e, err := store.Decode[domain.Execution](d)
	return &e, err
}
func retireCamera(tx *sql.Tx, o domain.Owner, id, reason string) error {
	e, err := activeCamera(tx, o, id)
	if err != nil || e == nil {
		return err
	}
	return store.Retire(tx, e, "cancelled", reason)
}
func (s *Server) mutateComposition(w http.ResponseWriter, r *http.Request, input any, change func([]domain.Member) ([]domain.Member, error)) {
	rev, err := revision(r)
	if err != nil {
		s.fail(w, err)
		return
	}
	var changed *domain.Execution
	s.command(w, r, input, func(tx *sql.Tx, op *domain.Operation) error {
		c, err := store.Camera(r.Context(), tx, principal(r).Owner, r.PathValue("id"))
		if err != nil {
			return err
		}
		if c.Revision != rev {
			return domain.Stale
		}
		members, err := change(c.Members)
		if err != nil {
			return err
		}
		members, err = s.Catalog.Validate(members, "stream")
		if err != nil {
			return err
		}
		for i := range members {
			for _, old := range c.Members {
				if members[i].ID == old.ID && members[i].Model == old.Model && members[i].Version == old.Version && members[i].FPS == old.FPS && domain.JSON(members[i].Config) == domain.JSON(old.Config) {
					members[i].Generation = old.Generation
					members[i].State = old.State
				}
			}
		}
		e, err := activeCamera(tx, c.Owner, c.ID)
		if err != nil {
			return err
		}
		if e != nil {
			if (e.State != "queued" && e.State != "running") || e.Kind != "stream" || (e.State == "running" && e.ObservedRevision < e.Revision) {
				return domain.Conflict
			}
			for i := range members {
				for _, old := range e.Members {
					if members[i].ID == old.ID && members[i].Model == old.Model && members[i].Version == old.Version && members[i].FPS == old.FPS && domain.JSON(members[i].Config) == domain.JSON(old.Config) {
						members[i].Generation = old.Generation
						members[i].State = old.State
					}
				}
			}
			if e.State != "queued" {
				oldGlobals, newGlobals := map[string]string{}, map[string]string{}
				for _, member := range e.Members {
					if catalog.GlobalRuntime(member.Model) {
						oldGlobals[member.ID] = domain.JSON(member)
					}
				}
				for _, member := range members {
					if catalog.GlobalRuntime(member.Model) {
						newGlobals[member.ID] = domain.JSON(member)
					}
				}
				if domain.JSON(oldGlobals) != domain.JSON(newGlobals) {
					return domain.Error(409, "runtime_restart_required", "El núcleo global requiere detener la ejecución antes de cambiar sus miembros")
				}
			}
			if e.State != "queued" {
				var held int
				if err = tx.QueryRow("SELECT memory_mib FROM reservations WHERE environment=? AND tenant_id=? AND execution_id=?", e.Environment, e.TenantID, e.ID).Scan(&held); err != nil {
					return err
				}
				if _, err = tx.Exec("DELETE FROM reservations WHERE environment=? AND tenant_id=? AND execution_id=?", e.Environment, e.TenantID, e.ID); err != nil {
					return err
				}
				if err = s.Reconciler.Budget.Reserve(r.Context(), tx, *e, held+s.Catalog.Cost(members)); err != nil {
					return err
				}
			}
			e.Members = members
			e.Revision = c.Revision + 1
			if err = store.SaveExecution(tx, *e); err != nil {
				return err
			}
			changed = e
			op.State = "pending"
		}
		c.Members = members
		c.Revision++
		if err = store.SaveCamera(tx, c); err != nil {
			return err
		}
		op.ResourceID = c.ID
		op.Result = c
		return nil
	})
	if changed != nil {
		if err := s.Reconciler.Configure(r.Context(), *changed); err != nil {
			_ = s.Store.Tx(context.Background(), func(tx *sql.Tx) error {
				return retireCamera(tx, changed.Owner, changed.CameraID, "composition_delivery_failed")
			})
		}
	}
}
func (s *Server) composition(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Members []domain.Member `json:"members"`
	}
	if err := read(w, r, &in); err != nil {
		s.fail(w, err)
		return
	}
	s.mutateComposition(w, r, in, func([]domain.Member) ([]domain.Member, error) { return in.Members, nil })
}
func (s *Server) addMember(w http.ResponseWriter, r *http.Request) {
	var in domain.Member
	if err := read(w, r, &in); err != nil {
		s.fail(w, err)
		return
	}
	s.mutateComposition(w, r, in, func(ms []domain.Member) ([]domain.Member, error) {
		for _, m := range ms {
			if m.ID == in.ID {
				return nil, domain.Conflict
			}
		}
		return append(ms, in), nil
	})
}
func (s *Server) patchMember(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Config *map[string]any `json:"config"`
		FPS    *float64        `json:"fps"`
		Policy *string         `json:"resident_policy"`
	}
	if err := read(w, r, &in); err != nil {
		s.fail(w, err)
		return
	}
	s.mutateComposition(w, r, in, func(ms []domain.Member) ([]domain.Member, error) {
		for i := range ms {
			if ms[i].ID == r.PathValue("member") {
				if in.Config != nil {
					ms[i].Config = *in.Config
				}
				if in.FPS != nil {
					ms[i].FPS = *in.FPS
				}
				if in.Policy != nil {
					ms[i].Policy = *in.Policy
				}
				return ms, nil
			}
		}
		return nil, domain.NotFound
	})
}
func (s *Server) deleteMember(w http.ResponseWriter, r *http.Request) {
	s.mutateComposition(w, r, map[string]string{"member_id": r.PathValue("member")}, func(ms []domain.Member) ([]domain.Member, error) {
		for i := range ms {
			if ms[i].ID == r.PathValue("member") {
				return append(ms[:i], ms[i+1:]...), nil
			}
		}
		return nil, domain.NotFound
	})
}
func (s *Server) newExecution(ctx context.Context, tx *sql.Tx, c domain.Camera, op *domain.Operation, kind string, queueSeconds int, members []domain.Member) (domain.Execution, error) {
	e := domain.Execution{}
	if !c.Enabled {
		return e, domain.Error(409, "camera_disabled", "Reactiva la cámara antes de solicitar trabajo")
	}
	if len(members) == 0 {
		return e, domain.Error(422, "composition_empty", "Asigna una composición")
	}
	var count int
	var q string
	if err := tx.QueryRowContext(ctx, "SELECT quota FROM tenants WHERE environment=? AND tenant_id=?", c.Environment, c.TenantID).Scan(&q); err != nil {
		return e, err
	}
	quota, err := store.Decode[domain.Quota](q)
	if err != nil {
		return e, err
	}
	if err = tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM executions WHERE environment=? AND tenant_id=? AND state='queued'", c.Environment, c.TenantID).Scan(&count); err != nil {
		return e, err
	}
	if count >= quota.Queue {
		return e, domain.Error(409, "queue_full", "Cola del tenant llena")
	}
	for i := range members {
		members[i].Generation = domain.ID()
		members[i].State = "pending"
	}
	e = domain.Execution{Owner: c.Owner, ID: domain.ID(), CameraID: c.ID, Generation: domain.ID(), Revision: c.Revision, State: "queued", Desired: "run", Kind: kind, Members: members, CreatedAt: domain.Now(), Deadline: time.Now().Add(time.Duration(queueSeconds) * time.Second).UTC().Format(time.RFC3339Nano), OperationID: op.ID}
	if err = store.InsertExecution(tx, e); err != nil {
		return e, err
	}
	op.State = "pending"
	op.ResourceID = e.ID
	op.Result = e
	return e, nil
}
func (s *Server) start(w http.ResponseWriter, r *http.Request) {
	var in struct {
		QueueSeconds int `json:"queue_seconds"`
	}
	if err := read(w, r, &in); err != nil {
		s.fail(w, err)
		return
	}
	if in.QueueSeconds == 0 {
		in.QueueSeconds = 60
	}
	if in.QueueSeconds < 1 || in.QueueSeconds > 3600 {
		s.fail(w, domain.Error(422, "queue_deadline", "queue_seconds debe ser 1–3600"))
		return
	}
	s.command(w, r, in, func(tx *sql.Tx, op *domain.Operation) error {
		c, err := store.Camera(r.Context(), tx, principal(r).Owner, r.PathValue("id"))
		if err != nil {
			return err
		}
		_, err = s.newExecution(r.Context(), tx, c, op, "stream", in.QueueSeconds, append([]domain.Member(nil), c.Members...))
		return err
	})
}
func (s *Server) setEnabled(w http.ResponseWriter, r *http.Request, enabled bool) {
	rev, err := revision(r)
	if err != nil {
		s.fail(w, err)
		return
	}
	s.command(w, r, map[string]bool{"enabled": enabled}, func(tx *sql.Tx, op *domain.Operation) error {
		c, err := store.Camera(r.Context(), tx, principal(r).Owner, r.PathValue("id"))
		if err != nil {
			return err
		}
		if c.Revision != rev {
			return domain.Stale
		}
		c.Enabled = enabled
		c.Revision++
		if !enabled {
			if err = retireCamera(tx, c.Owner, c.ID, "camera_disconnected"); err != nil {
				return err
			}
			e, err := activeCamera(tx, c.Owner, c.ID)
			if err != nil {
				return err
			}
			if e != nil {
				op.State = "pending"
			}
		}
		if err = store.SaveCamera(tx, c); err != nil {
			return err
		}
		op.ResourceID = c.ID
		op.Result = c
		return nil
	})
}
func (s *Server) disconnect(w http.ResponseWriter, r *http.Request) { s.setEnabled(w, r, false) }
func (s *Server) reactivate(w http.ResponseWriter, r *http.Request) { s.setEnabled(w, r, true) }
func (s *Server) execution(w http.ResponseWriter, r *http.Request) {
	e, err := s.Store.Execution(r.Context(), principal(r).Owner, r.PathValue("id"))
	if err != nil {
		s.fail(w, err)
		return
	}
	write(w, 200, e)
}
func (s *Server) operation(w http.ResponseWriter, r *http.Request) {
	op, err := s.Store.Operation(r.Context(), principal(r).Owner, r.PathValue("id"))
	if err != nil {
		s.fail(w, err)
		return
	}
	write(w, 200, op)
}
func (s *Server) stop(w http.ResponseWriter, r *http.Request) {
	s.command(w, r, map[string]string{}, func(tx *sql.Tx, op *domain.Operation) error {
		e, err := store.Execution(r.Context(), tx, principal(r).Owner, r.PathValue("id"))
		if err != nil {
			return err
		}
		if err = store.Retire(tx, &e, "cancelled", "execution_stop"); err != nil {
			return err
		}
		if e.Active() {
			op.State = "pending"
		}
		op.ResourceID = e.ID
		op.Result = e
		return nil
	})
}
func (s *Server) clearQueue(w http.ResponseWriter, r *http.Request) {
	s.command(w, r, map[string]string{}, func(tx *sql.Tx, op *domain.Operation) error {
		o := principal(r).Owner
		rows, err := tx.Query("SELECT data FROM executions WHERE environment=? AND tenant_id=? AND state='queued'", o.Environment, o.TenantID)
		if err != nil {
			return err
		}
		var es []domain.Execution
		for rows.Next() {
			var d string
			if err = rows.Scan(&d); err != nil {
				rows.Close()
				return err
			}
			e, err := store.Decode[domain.Execution](d)
			if err != nil {
				rows.Close()
				return err
			}
			es = append(es, e)
		}
		rows.Close()
		for _, e := range es {
			if err = store.Retire(tx, &e, "cancelled", "queue_clear"); err != nil {
				return err
			}
			if err = store.UpdateOperation(tx, e.Owner, e.OperationID, "succeeded", e); err != nil {
				return err
			}
		}
		op.Result = map[string]int{"cancelled": len(es)}
		return nil
	})
}
func (s *Server) prediction(w http.ResponseWriter, r *http.Request) {
	var in struct {
		CameraID     string          `json:"camera_id"`
		Kind         string          `json:"kind"`
		Members      []domain.Member `json:"members"`
		Media        string          `json:"media_base64"`
		QueueSeconds int             `json:"queue_seconds"`
	}
	if err := read(w, r, &in); err != nil {
		s.fail(w, err)
		return
	}
	if in.Kind != "image" && in.Kind != "clip" {
		s.fail(w, domain.Error(422, "prediction_kind", "kind requiere image o clip"))
		return
	}
	b, err := base64.StdEncoding.DecodeString(in.Media)
	if err != nil || len(b) == 0 || len(b) > 400*1024 {
		s.fail(w, domain.Error(413, "prediction_size", "El medio debe tener entre 1 byte y 400 KiB"))
		return
	}
	members, err := s.Catalog.Validate(in.Members, in.Kind)
	if err != nil {
		s.fail(w, err)
		return
	}
	if in.QueueSeconds == 0 {
		in.QueueSeconds = 60
	}
	if in.QueueSeconds < 1 || in.QueueSeconds > 3600 {
		s.fail(w, domain.Error(422, "queue_deadline", "queue_seconds fuera de rango"))
		return
	}
	s.command(w, r, in, func(tx *sql.Tx, op *domain.Operation) error {
		c, err := store.Camera(r.Context(), tx, principal(r).Owner, in.CameraID)
		if err != nil {
			return err
		}
		e, err := s.newExecution(r.Context(), tx, c, op, in.Kind, in.QueueSeconds, members)
		if err != nil {
			return err
		}
		encrypted, err := s.Store.Seal(c.Owner, in.Media)
		if err != nil {
			return err
		}
		_, err = tx.Exec("INSERT INTO execution_media VALUES(?,?,?,?)", e.Environment, e.TenantID, e.ID, encrypted)
		return err
	})
}
func (s *Server) issueKey(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Scopes    []string `json:"scopes"`
		ExpiresAt string   `json:"expires_at"`
	}
	if err := read(w, r, &in); err != nil {
		s.fail(w, err)
		return
	}
	p := principal(r)
	for _, scope := range in.Scopes {
		if !p.Can(scope) {
			s.fail(w, domain.Error(403, "scope_escalation", "No puedes conceder scopes que no tienes"))
			return
		}
	}
	expiry, err := time.Parse(time.RFC3339, in.ExpiresAt)
	if err != nil {
		s.fail(w, domain.Error(422, "expires_at", "Caducidad inválida"))
		return
	}
	id, token, h, err := auth.Prepare(in.Scopes, expiry)
	if err != nil {
		s.fail(w, err)
		return
	}
	secret := ""
	op, err := s.Store.Command(r.Context(), p, r.Header.Get("Idempotency-Key"), s.Store.Sign(r.Method+" "+r.URL.Path+" "+domain.JSON(in)), "credential.issue", w.Header().Get("X-Request-ID"), func(tx *sql.Tx, op *domain.Operation) error {
		if _, err := tx.Exec("INSERT INTO credentials(id,environment,tenant_id,hash,scopes,expires_at) VALUES(?,?,?,?,?,?)", id, p.Environment, p.TenantID, h, domain.JSON(in.Scopes), expiry.UTC().Format(time.RFC3339Nano)); err != nil {
			return err
		}
		secret = token
		op.ResourceID = id
		op.Result = map[string]any{"id": id, "scopes": in.Scopes, "expires_at": in.ExpiresAt}
		return nil
	})
	if err != nil {
		s.fail(w, err)
		return
	}
	write(w, 201, map[string]any{"operation": op, "key": secret})
}
func (s *Server) revokeKey(w http.ResponseWriter, r *http.Request) {
	s.command(w, r, map[string]string{}, func(tx *sql.Tx, op *domain.Operation) error {
		o := principal(r).Owner
		res, err := tx.Exec("UPDATE credentials SET revoked=1 WHERE environment=? AND tenant_id=? AND id=?", o.Environment, o.TenantID, r.PathValue("id"))
		if err != nil {
			return err
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			return domain.NotFound
		}
		op.ResourceID = r.PathValue("id")
		op.Result = map[string]bool{"revoked": true}
		return nil
	})
}
