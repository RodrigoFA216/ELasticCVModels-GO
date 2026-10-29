package catalog

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"sort"

	"github.com/santhosh-tekuri/jsonschema/v6"
	"kinich-go/internal/domain"
)

//go:embed catalog.json
var raw []byte

type Catalog struct {
	Models  map[string]domain.Model
	schemas map[string]*jsonschema.Schema
}

func Load(profilePath string) (*Catalog, error) {
	var models []domain.Model
	if err := json.Unmarshal(raw, &models); err != nil {
		return nil, err
	}
	profiles := map[string]domain.Profile{}
	if profilePath != "" {
		b, err := os.ReadFile(profilePath)
		if err != nil {
			return nil, err
		}
		if err = json.Unmarshal(b, &profiles); err != nil {
			return nil, err
		}
	}
	c := &Catalog{map[string]domain.Model{}, map[string]*jsonschema.Schema{}}
	for _, m := range models {
		if p, ok := profiles[m.Name]; ok {
			if !p.Measured || p.ResidentMiB < 0 || p.SessionMiB <= 0 || p.WarmupMiB < 0 || p.InFlight != 1 || p.Hardware == "" {
				return nil, fmt.Errorf("perfil no medido o inválido: %s", m.Name)
			}
			m.Profile = &p
			m.Available = true
			m.Reason = "deployment_profile_requires_asset_validation"
		}
		if m.Name == "combo" {
			m.Available = false
			m.Profile = nil
		}
		comp := jsonschema.NewCompiler()
		u := "https://kinich.invalid/schema/" + m.Name
		if err := comp.AddResource(u, m.ConfigSchema); err != nil {
			return nil, err
		}
		schema, err := comp.Compile(u)
		if err != nil {
			return nil, err
		}
		c.Models[m.Name] = m
		c.schemas[m.Name] = schema
	}
	return c, nil
}
func (c *Catalog) List() []domain.Model {
	out := make([]domain.Model, 0, len(c.Models))
	for _, m := range c.Models {
		out = append(out, m)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}
func (c *Catalog) Validate(ms []domain.Member, capability string) ([]domain.Member, error) {
	if len(ms) < 1 || len(ms) > 16 {
		return nil, domain.Error(422, "invalid_composition", "Se requieren de 1 a 16 miembros")
	}
	seen := map[string]bool{}
	globalRuntime := ""
	out := make([]domain.Member, 0, len(ms))
	for _, m := range ms {
		if !regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,64}$`).MatchString(m.ID) || seen[m.ID] {
			return nil, domain.Error(422, "invalid_member", "member_id debe ser único y tener 1–64 caracteres")
		}
		seen[m.ID] = true
		def, ok := c.Models[m.Model]
		if !ok || m.Model == "combo" {
			return nil, domain.Error(422, "unknown_model", "Modelo desconocido; combo se expresa con miembros")
		}
		if m.Version != "" && m.Version != def.Version {
			return nil, domain.Error(422, "model_version", "Versión incompatible")
		}
		m.Version = def.Version
		if !def.Available || def.Profile == nil {
			return nil, domain.Error(409, "unprofiled_model", "El modelo requiere activos y perfil medido: "+m.Model)
		}
		has := false
		for _, cap := range def.Capabilities {
			if cap == capability {
				has = true
			}
		}
		if !has {
			return nil, domain.Error(422, "capability", "El modelo no admite "+capability)
		}
		if m.Config == nil {
			m.Config = map[string]any{}
		}
		if err := c.schemas[m.Model].Validate(m.Config); err != nil {
			return nil, domain.Error(422, "invalid_config", fmt.Sprintf("Configuración no válida para %s: %s", m.Model, err))
		}
		if m.Model == "corridor-counter" {
			v, ok := m.Config["box"].([]any)
			if !ok || len(v) != 4 {
				return nil, domain.Error(422, "invalid_geometry", "box requiere exactamente cuatro puntos")
			}
		}
		if GlobalRuntime(m.Model) {
			if globalRuntime != "" {
				return nil, domain.Error(409, "runtime_global_conflict", "El núcleo global requiere miembros en procesos separados")
			}
			globalRuntime = m.Model
		}

		if m.FPS == 0 {
			m.FPS = def.TargetFPS
		}
		if m.FPS < 0.1 || m.FPS > def.TargetFPS {
			return nil, domain.Error(422, "invalid_cadence", "FPS fuera del perfil del modelo")
		}
		if m.Policy == "" {
			m.Policy = "evictable"
		}
		if m.Policy != "evictable" && m.Policy != "warm" && m.Policy != "pinned" {
			return nil, domain.Error(422, "resident_policy", "Política inválida")
		}
		m.Generation = domain.ID()
		m.State = "pending"
		out = append(out, m)
	}
	return out, nil
}
func (c *Catalog) Cost(ms []domain.Member) int {
	cost := 0
	for _, m := range ms {
		p := c.Models[m.Model].Profile
		if p == nil {
			return -1
		}
		cost += p.SessionMiB + p.WarmupMiB + p.ResidentMiB
	}
	return cost
}

func GlobalRuntime(model string) bool {
	return model == "whitegas" || model == "ppe-detector" || model == "queue-cashier" || model == "corridor-counter"
}
