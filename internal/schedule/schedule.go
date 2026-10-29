package schedule

import (
	"context"
	"database/sql"
	"fmt"
	"slices"
	"time"
	_ "time/tzdata"

	"kinich-go/internal/domain"
	"kinich-go/internal/store"
)

func Validate(s domain.Schedule) error {
	if s.Hour < 0 || s.Hour > 23 || s.Minute < 0 || s.Minute > 59 || len(s.Weekdays) < 1 || len(s.Weekdays) > 7 || s.Overlap != "skip" {
		return domain.Error(422, "invalid_schedule", "Hora, días o política de solapamiento inválidos (sólo skip)")
	}
	for _, d := range s.Weekdays {
		if d < 0 || d > 6 {
			return domain.Error(422, "invalid_schedule", "Días deben estar entre 0 y 6")
		}
	}
	if _, err := time.LoadLocation(s.Timezone); err != nil {
		return domain.Error(422, "invalid_timezone", "Se requiere zona IANA")
	}
	return nil
}
func Occurrence(s domain.Schedule, now time.Time) (string, bool) {
	loc, err := time.LoadLocation(s.Timezone)
	if err != nil {
		return "", false
	}
	local := now.In(loc)
	if !s.Enabled || local.Hour() != s.Hour || local.Minute() != s.Minute || !slices.Contains(s.Weekdays, int(local.Weekday())) {
		return "", false
	}
	return local.Format("2006-01-02") + fmt.Sprintf("T%02d:%02d", s.Hour, s.Minute), true
}

type Scheduler struct {
	Store *store.Store
	Start func(context.Context, *sql.Tx, domain.Camera, *domain.Operation) error
}

func (s *Scheduler) Run(ctx context.Context) {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-t.C:
			_ = s.Tick(ctx, now)
		}
	}
}
func (s *Scheduler) Tick(ctx context.Context, now time.Time) error {
	rows, err := s.Store.DB.QueryContext(ctx, "SELECT data FROM schedules")
	if err != nil {
		return err
	}
	ss := []domain.Schedule{}
	for rows.Next() {
		var d string
		if err = rows.Scan(&d); err != nil {
			rows.Close()
			return err
		}
		v, err := store.Decode[domain.Schedule](d)
		if err != nil {
			rows.Close()
			return err
		}
		ss = append(ss, v)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, sc := range ss {
		occ, ok := Occurrence(sc, now)
		if !ok {
			continue
		}
		err = s.Store.Tx(ctx, func(tx *sql.Tx) error {
			var current string
			if err := tx.QueryRowContext(ctx, "SELECT data FROM schedules WHERE environment=? AND tenant_id=? AND id=?", sc.Environment, sc.TenantID, sc.ID).Scan(&current); err != nil {
				return store.Missing(err)
			}
			latest, err := store.Decode[domain.Schedule](current)
			if err != nil {
				return err
			}
			if latest.Revision != sc.Revision || !latest.Enabled {
				return nil
			}
			res, err := tx.ExecContext(ctx, "INSERT OR IGNORE INTO occurrences VALUES(?,?,?,?,?)", sc.Environment, sc.TenantID, sc.ID, occ, "skipped")
			if err != nil {
				return err
			}
			n, _ := res.RowsAffected()
			if n == 0 {
				return nil
			}
			c, err := store.Camera(ctx, tx, sc.Owner, sc.CameraID)
			if err != nil {
				return err
			}
			var active int
			if err = tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM executions WHERE environment=? AND tenant_id=? AND camera_id=? AND state IN ('queued','preparing','running','stopping')", sc.Environment, sc.TenantID, sc.CameraID).Scan(&active); err != nil {
				return err
			}
			if !c.Enabled || active > 0 || len(c.Members) == 0 {
				return nil
			}
			op := domain.Operation{Owner: sc.Owner, ID: domain.ID(), RequestID: domain.ID(), Kind: "schedule", State: "pending", CreatedAt: domain.Now()}
			if err = s.Start(ctx, tx, c, &op); err != nil {
				return err
			}
			if _, err = tx.ExecContext(ctx, "INSERT INTO operations VALUES(?,?,?,?,?,?)", op.ID, sc.Environment, sc.TenantID, "schedule:"+sc.ID+":"+occ, store.Digest(occ), domain.JSON(op)); err != nil {
				return err
			}
			_, err = tx.ExecContext(ctx, "UPDATE occurrences SET outcome='queued' WHERE environment=? AND tenant_id=? AND schedule_id=? AND occurrence=?", sc.Environment, sc.TenantID, sc.ID, occ)
			return err
		})
		if err != nil {
			return err
		}
	}
	return nil
}
