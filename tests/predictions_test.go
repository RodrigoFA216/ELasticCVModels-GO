package tests

import (
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"kinich-go/internal/domain"
)

func TestImageAndClipPredictionsWithCVRuntime(t *testing.T) {
	python := os.Getenv("KINICH_CV_PYTHON")
	if python == "" {
		t.Skip("KINICH_CV_PYTHON requiere Python con OpenCV para predicciones CPU")
	}
	data, err := exec.Command(python, "-c", `import cv2,numpy as np,tempfile,os,base64,json
frame=np.zeros((120,160,3),np.uint8)
ok,image=cv2.imencode('.jpg',frame)
fd,path=tempfile.mkstemp(suffix='.mp4');os.close(fd)
w=cv2.VideoWriter(path,cv2.VideoWriter_fourcc(*'mp4v'),10,(160,120))
for i in range(10):w.write(frame)
w.release()
print(json.dumps({'image':base64.b64encode(image).decode(),'clip':base64.b64encode(open(path,'rb').read()).decode()}));os.unlink(path)`).Output()
	if err != nil {
		t.Fatal(err)
	}
	var media map[string]string
	if err = json.Unmarshal(data, &media); err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"image", "clip"} {
		t.Run(kind, func(t *testing.T) {
			f := setup(t)
			f.rec.Manager.Python = python
			// El test habilita IMAGE sólo para el diagnóstico CPU; no certifica un modelo real.
			def := f.server.Catalog.Models["pulso"]
			def.Capabilities = append(def.Capabilities, "image")
			f.server.Catalog.Models["pulso"] = def
			c := f.camera(0, "PREDICT", "prediction-camera", nil)
			status, b := f.request(0, "POST", "/v2/predictions", "prediction-start", 0, map[string]any{"camera_id": c.ID, "kind": kind, "members": []domain.Member{pulse("p")}, "media_base64": media[kind]})
			if status != 202 {
				t.Fatalf("%d %s", status, b)
			}
			op := decode[domain.Operation](t, b)
			e := decode[domain.Execution](t, []byte(domain.JSON(op.Result)))
			f.run()
			eventually(t, 10*time.Second, func() bool {
				current, _ := f.store.Execution(f.ctx, e.Owner, e.ID)
				if current.State == "failed" {
					t.Fatalf("prediction failed: %s", current.Reason)
				}
				return current.State == "completed"
			})
			es, err := f.server.Events.List(f.ctx, e.Owner, e.ID, 0, 100)
			if err != nil || len(es) == 0 {
				t.Fatal("prediction lost events", err)
			}
			var count int
			f.store.DB.QueryRow("SELECT COUNT(*) FROM execution_media").Scan(&count)
			if count != 0 {
				t.Fatal("temporary media retained after cleanup")
			}
			status, b = f.request(1, "POST", "/v2/predictions", "foreign-prediction", 0, map[string]any{"camera_id": c.ID, "kind": kind, "members": []domain.Member{pulse("p")}, "media_base64": media[kind]})
			if status != 404 {
				t.Fatalf("foreign predict %d %s", status, b)
			}
			if strings.Contains(domain.JSON(es), media[kind]) {
				t.Fatal("media leaked")
			}
		})
	}
}
