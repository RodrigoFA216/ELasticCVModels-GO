# Adaptador VMS

`internal/integrations/vms` ofrece un cliente de servidor /v2 y traducción de evento a alerta por UUID con resolver autenticado. La resolución exige el mismo environment/tenant; ni RTSP ni code eligen propietario. El cliente no sigue redirects ni devuelve errores con fuentes/tokens.

ToAlert conserva evento, ejecución, miembro, modelo, estado semántico, geometría y dueño. `_OFF` cierra un fenómeno y no representa resolución humana. Evidencia comienza pending; recibir un evento no significa video finalizado. VMS debe finalizar manifest/escritor o marcar incompleto al desconectar/fallar.

El receptor VMS histórico del 08/10 acepta source y no este contrato estable v2. Se necesita integrar el adaptador en su servidor y un receptor que deduplique por owner/cámara/ejecución/evento antes de persistir alerta. Este repositorio no modificó ni desplegó el VMS existente y no afirma que ese recorrido real esté validado.

Las grabaciones y cuotas pertenecen a VMS. Nunca borrar medios ajenos ni atribuir históricos a una reasignación posterior de cámara. El ensayo pendiente debe cubrir receptor lento, `_OFF`, EPP verificando, escritor caído y archivos incompletos con dos tenants.
