package api

import (
	"context"
	"database/sql"
	"net/http"

	"kinich-go/internal/domain"
	"kinich-go/internal/schedule"
	"kinich-go/internal/store"
)

func (s *Server) Scheduler() *schedule.Scheduler {
	return &schedule.Scheduler{Store: s.Store, Start: func(ctx context.Context, tx *sql.Tx, c domain.Camera, op *domain.Operation) error {
		_, err := s.newExecution(ctx, tx, c, op, "stream", 60, append([]domain.Member(nil), c.Members...))
		return err
	}}
}
func (s *Server) schedules(w http.ResponseWriter, r *http.Request) {
	o := principal(r).Owner
	rows, err := s.Store.DB.QueryContext(r.Context(), "SELECT data FROM schedules WHERE environment=? AND tenant_id=? LIMIT 1000", o.Environment, o.TenantID)
	if err != nil {
		s.fail(w, err)
		return
	}
	defer rows.Close()
	out := []domain.Schedule{}
	for rows.Next() {
		var d string
		if err = rows.Scan(&d); err != nil {
			s.fail(w, err)
			return
		}
		sc, err := store.Decode[domain.Schedule](d)
		if err != nil {
			s.fail(w, err)
			return
		}
		out = append(out, sc)
	}
	write(w, 200, out)
}

type scheduleInput struct {
	CameraID string `json:"camera_id"`
	Timezone string `json:"timezone"`
	Hour     int    `json:"hour"`
	Minute   int    `json:"minute"`
	Weekdays []int  `json:"weekdays"`
	Enabled  bool   `json:"enabled"`
	Overlap  string `json:"overlap"`
}

func (s *Server) createSchedule(w http.ResponseWriter, r *http.Request) { s.saveSchedule(w, r, false) }
func (s *Server) patchSchedule(w http.ResponseWriter, r *http.Request)  { s.saveSchedule(w, r, true) }
func (s *Server) saveSchedule(w http.ResponseWriter, r *http.Request, existing bool) {
	var in scheduleInput
	if err := read(w, r, &in); err != nil {
		s.fail(w, err)
		return
	}
	sc := domain.Schedule{Owner: principal(r).Owner, ID: domain.ID(), CameraID: in.CameraID, Revision: 1, Timezone: in.Timezone, Hour: in.Hour, Minute: in.Minute, Weekdays: in.Weekdays, Enabled: in.Enabled, Overlap: in.Overlap}
	if sc.Overlap == "" {
		sc.Overlap = "skip"
	}
	if err := schedule.Validate(sc); err != nil {
		s.fail(w, err)
		return
	}
	var rev int64
	var err error
	if existing {
		rev, err = revision(r)
		if err != nil {
			s.fail(w, err)
			return
		}
		sc.ID = r.PathValue("id")
		sc.Revision = rev + 1
	}
	s.command(w, r, in, func(tx *sql.Tx, op *domain.Operation) error {
		if _, err := store.Camera(r.Context(), tx, sc.Owner, sc.CameraID); err != nil {
			return err
		}
		if existing {
			var d string
			if err := tx.QueryRow("SELECT data FROM schedules WHERE environment=? AND tenant_id=? AND id=?", sc.Environment, sc.TenantID, sc.ID).Scan(&d); err != nil {
				return store.Missing(err)
			}
			old, err := store.Decode[domain.Schedule](d)
			if err != nil {
				return err
			}
			if old.Revision != rev {
				return domain.Stale
			}
			if old.CameraID != sc.CameraID {
				return domain.Error(422, "immutable_camera", "La cámara del horario es inmutable")
			}
			_, err = tx.Exec("UPDATE schedules SET data=? WHERE environment=? AND tenant_id=? AND id=?", domain.JSON(sc), sc.Environment, sc.TenantID, sc.ID)
			if err != nil {
				return err
			}
		} else {
			var n int
			if err := tx.QueryRow("SELECT COUNT(*) FROM schedules WHERE environment=? AND tenant_id=?", sc.Environment, sc.TenantID).Scan(&n); err != nil {
				return err
			}
			if n >= 1000 {
				return domain.Error(409, "schedule_quota", "Límite de horarios alcanzado")
			}
			if _, err := tx.Exec("INSERT INTO schedules VALUES(?,?,?,?,?)", sc.ID, sc.Environment, sc.TenantID, sc.CameraID, domain.JSON(sc)); err != nil {
				return err
			}
		}
		op.ResourceID = sc.ID
		op.Result = sc
		return nil
	})
}
func (s *Server) deleteSchedule(w http.ResponseWriter, r *http.Request) {
	rev, err := revision(r)
	if err != nil {
		s.fail(w, err)
		return
	}
	s.command(w, r, map[string]string{}, func(tx *sql.Tx, op *domain.Operation) error {
		o := principal(r).Owner
		var d string
		if err := tx.QueryRow("SELECT data FROM schedules WHERE environment=? AND tenant_id=? AND id=?", o.Environment, o.TenantID, r.PathValue("id")).Scan(&d); err != nil {
			return store.Missing(err)
		}
		sc, err := store.Decode[domain.Schedule](d)
		if err != nil {
			return err
		}
		if sc.Revision != rev {
			return domain.Stale
		}
		sc.Enabled = false
		sc.Revision++
		_, err = tx.Exec("UPDATE schedules SET data=? WHERE environment=? AND tenant_id=? AND id=?", domain.JSON(sc), o.Environment, o.TenantID, sc.ID)
		op.ResourceID = sc.ID
		op.Result = sc
		return err
	})
}
