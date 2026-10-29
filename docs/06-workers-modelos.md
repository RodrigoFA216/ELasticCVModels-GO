# Workers y modelos

El runtime nuevo está en `worker/kinich_worker`. `worker/vendor/kinich/models` contiene únicamente código de analíticas y helpers de la referencia; su paquete `__init__` no registra ni precarga modelos. No se importa FastAPI, registry, managers, Redis ni SQLite del motor anterior.

Cada miembro instancia su modelo y sesión; no se asume que un predictor YOLO mutable se pueda compartir entre hilos. El directorio de trabajo es privado por worker. Los contadores de corredor usan raíz persistente por hash de propietario/cámara y member_id hasheado. No se adopta nada por RTSP ni se comparte captura entre tenants.

El catálogo niega modelos reales sin perfil medido. Para habilitar uno: aprovisionar los activos listados en su ficha, validar versión/pesos/dependencias, medir el proceso completo y generar un JSON `modelo → {resident_mib,session_mib,warmup_mib,in_flight:1,measured:true,hardware}`. Arrancar con `-profiles /ruta/perfiles.json` y `KINICH_ALLOW_REAL_MODELS=1`. El flag no prueba paridad: sólo habilita la carga explícita.

SAM/ComfyUI, ONNX/ReID, galerías y pesos no están en el repositorio. La configuración global incompatible se rechaza. Una biblioteca nativa bloqueada afecta la cámara del worker; el supervisor termina su grupo y mantiene recursos pendientes hasta comprobar salida.

Al reiniciar, las generaciones previas quedan en stopping y no se renuevan. Si el grupo anterior ya no existe, se confirma el fallo y se libera; si su identidad/salida sigue incierta, permanece pendiente. El caso PID no registrado conserva reserva y requiere reconciliación operativa. El servicio Linux usa KillMode=control-group para cubrir reinicios del controlador.

El adaptador verifica nombre y versión del modelo cargado antes de abrir sesión. Los núcleos globales no admiten sustitución dinámica dentro del proceso existente; se exige parada/arranque explícito. El resto de miembros conserva su estado durante cambios compatibles.
