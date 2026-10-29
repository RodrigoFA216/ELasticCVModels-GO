package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"kinich-go/internal/admission"
	"kinich-go/internal/api"
	"kinich-go/internal/auth"
	"kinich-go/internal/catalog"
	"kinich-go/internal/domain"
	"kinich-go/internal/events"
	"kinich-go/internal/reconcile"
	"kinich-go/internal/store"
	"kinich-go/internal/supervisor"
)

var version = "0.1.0-dev"

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "kinichd:", err)
		os.Exit(1)
	}
}
func run() error {
	command := flag.String("command", "serve", "init | serve | tenant | backup | integrity | version")
	state := flag.String("state", ".state", "Directorio privado de estado")
	listen := flag.String("listen", "127.0.0.1:8090", "Dirección HTTP")
	environment := flag.String("environment", "development", "Entorno para init/tenant")
	tenantID := flag.String("tenant", "local", "Tenant para init/tenant")
	python := flag.String("python", "python3", "Ejecutable Python")
	worker := flag.String("worker-dir", "worker", "Directorio runtime Python")
	profiles := flag.String("profiles", "", "Perfiles medidos por modelo")
	memory := flag.Int("memory-mib", 1024, "Presupuesto medido de memoria")
	maxWorkers := flag.Int("max-workers", 8, "Límite de procesos")
	synthetic := flag.Bool("allow-synthetic", false, "Permitir synthetic:// para pruebas explícitas")
	callbackCIDRs := flag.String("callback-cidrs", "", "Redes LAN receptoras aprobadas (CIDR)")
	callbackHTTP := flag.Bool("callback-http", false, "Permitir HTTP en despliegue interno controlado")
	backup := flag.String("backup-path", "", "Destino nuevo del respaldo")
	flag.Parse()
	if *command == "version" {
		fmt.Println(version)
		return nil
	}
	if *memory < 16 || *maxWorkers < 1 || *maxWorkers > 128 {
		return errors.New("presupuesto o workers inválidos")
	}
	abs, err := filepath.Abs(*state)
	if err != nil {
		return err
	}
	if err = os.MkdirAll(abs, 0700); err != nil {
		return err
	}
	if err = os.Chmod(abs, 0700); err != nil {
		return err
	}
	keyPath := filepath.Join(abs, "master.key")
	if *command == "init" {
		key := make([]byte, 32)
		if _, err = rand.Read(key); err != nil {
			return err
		}
		f, err := os.OpenFile(keyPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			return fmt.Errorf("init requiere estado nuevo: %w", err)
		}
		if _, err = f.Write(key); err != nil {
			f.Close()
			return err
		}
		if err = f.Close(); err != nil {
			return err
		}
	}
	key, err := os.ReadFile(keyPath)
	if err != nil {
		return fmt.Errorf("ejecuta -command init primero: %w", err)
	}
	info, err := os.Stat(keyPath)
	if err != nil {
		return err
	}
	if info.Mode().Perm()&0077 != 0 {
		return errors.New("master.key requiere permisos 0600")
	}
	st, err := store.Open(filepath.Join(abs, "kinich.sqlite"), key)
	if err != nil {
		return err
	}
	defer st.Close()
	ctx := context.Background()
	a := auth.Auth{Store: st}
	switch *command {
	case "init", "tenant":
		valid := regexp.MustCompile(`^[A-Za-z0-9_-]{1,80}$`)
		if !valid.MatchString(*environment) || !valid.MatchString(*tenantID) {
			return errors.New("entorno y tenant inválidos")
		}
		o := domain.Owner{Environment: *environment, TenantID: *tenantID}
		if *command == "tenant" {
			if _, err = st.Tenant(ctx, o); err == nil {
				return errors.New("tenant ya existe")
			}
		}
		t := domain.Tenant{Owner: o, Quota: domain.Quota{Cameras: 64, Executions: 4, MemoryMiB: *memory, Queue: 64}}
		if err = st.PutTenant(ctx, t); err != nil {
			return err
		}
		id, token, err := a.Issue(ctx, o, []string{"*"}, time.Now().Add(90*24*time.Hour))
		if err != nil {
			return err
		}
		path := filepath.Join(abs, "credential-"+o.Environment+"-"+o.TenantID+".json")
		data, _ := json.MarshalIndent(map[string]string{"id": id, "key": token, "environment": o.Environment, "tenant_id": o.TenantID}, "", "  ")
		if err = os.WriteFile(path, append(data, '\n'), 0600); err != nil {
			return err
		}
		fmt.Println("Credencial inicial guardada con permisos 0600:", path)
		return nil
	case "backup":
		if *backup == "" {
			return errors.New("se requiere -backup-path")
		}
		if err = st.Backup(ctx, *backup); err != nil {
			return err
		}
		fmt.Println("Respaldo SQLite creado; conservar master.key en el respaldo seguro separado")
		return nil
	case "integrity":
		var result string
		if err = st.DB.QueryRow("PRAGMA integrity_check").Scan(&result); err != nil {
			return err
		}
		if result != "ok" {
			return errors.New("integridad falló")
		}
		fmt.Println("SQLite: ok")
		return nil
	case "serve":
	default:
		return errors.New("comando desconocido")
	}
	cat, err := catalog.Load(*profiles)
	if err != nil {
		return err
	}
	workerDir, err := filepath.Abs(*worker)
	if err != nil {
		return err
	}
	if _, err = os.Stat(filepath.Join(workerDir, "kinich_worker", "runtime.py")); err != nil {
		return err
	}
	if err = st.Recover(ctx); err != nil {
		return err
	}
	manager := supervisor.New(*python, workerDir)
	manager.StateDir = filepath.Join(abs, "workers")
	ev := &events.Service{Store: st}
	cidrs, err := events.ParseCIDRs(*callbackCIDRs)
	if err != nil {
		return err
	}
	outbox := &events.Outbox{Store: st, Policy: events.NetworkPolicy{ApprovedCIDRs: cidrs, AllowHTTP: *callbackHTTP}}
	rec := &reconcile.Reconciler{Store: st, Catalog: cat, Manager: manager, Budget: admission.Budget{MemoryMiB: *memory, MaxWorkers: *maxWorkers}, Event: ev.Accept, Gate: admission.NewGate(2)}
	srv := &api.Server{Store: st, Auth: a, Catalog: cat, Reconciler: rec, Events: ev, Outbox: outbox, AllowSynthetic: *synthetic}
	workCtx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go rec.Run(workCtx)
	go outbox.Run(workCtx)
	go ev.Retain(workCtx, 30)
	go srv.Scheduler().Run(workCtx)
	httpServer := &http.Server{Addr: *listen, Handler: srv.Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 * 1024}
	signalCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	errC := make(chan error, 1)
	go func() {
		fmt.Println("Kinich", version, "HTTP", *listen, "Go control / Python models")
		errC <- httpServer.ListenAndServe()
	}()
	select {
	case <-signalCtx.Done():
	case err = <-errC:
		if !errors.Is(err, http.ErrServerClosed) {
			cancel()
			manager.Shutdown(context.Background())
			return err
		}
	}
	rec.StopAdmissions()
	shutdownCtx, done := context.WithTimeout(context.Background(), 10*time.Second)
	defer done()
	cancel()
	manager.Shutdown(shutdownCtx)
	_ = rec.Tick(shutdownCtx)
	if err = httpServer.Shutdown(shutdownCtx); err != nil && !strings.Contains(err.Error(), "context") {
		return err
	}
	return nil
}
