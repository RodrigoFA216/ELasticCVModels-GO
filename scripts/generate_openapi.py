"""Genera el inventario OpenAPI del mux; esquemas de entrada se mantienen explícitos."""
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[1]
S = lambda kind, **kw: dict(type=kind, **kw)
R = lambda name: {'$ref': '#/components/schemas/' + name}
def obj(properties, required=()):
    return dict(type='object', properties=properties, required=list(required), additionalProperties=False)

schemas = {
 'MemberInput': obj({'member_id': S('string', pattern='^[A-Za-z0-9_.:-]{1,64}$'), 'model': S('string'), 'version': S('string'), 'config': S('object'), 'fps': S('number', minimum=.1), 'resident_policy': S('string', enum=['pinned','warm','evictable'])}, ['member_id','model']),
 'CameraInput': obj({'code': S('string', minLength=1, maxLength=120), 'name': S('string', minLength=1, maxLength=200), 'source': S('string', writeOnly=True, maxLength=4096), 'members': S('array', items=R('MemberInput'), maxItems=16)}, ['code','name','source']),
 'CompositionInput': obj({'members': S('array', items=R('MemberInput'), minItems=1, maxItems=16)}, ['members']),
 'ExecutionInput': obj({'queue_seconds': S('integer', minimum=1, maximum=3600, default=60)}),
 'CameraPatch': obj({'name': S('string', minLength=1, maxLength=200), 'source': S('string', writeOnly=True, maxLength=4096)}),
 'MemberPatch': obj({'config': S('object'), 'fps': S('number', minimum=.1), 'resident_policy': S('string', enum=['pinned','warm','evictable'])}),
 'PredictionInput': obj({'camera_id': S('string', format='uuid'), 'kind': S('string', enum=['image','clip']), 'members': S('array', items=R('MemberInput'), minItems=1, maxItems=16), 'media_base64': S('string', contentEncoding='base64', description='Máximo 400 KiB decodificados; clips <= 60s/1800 frames'), 'queue_seconds': S('integer', minimum=1, maximum=3600, default=60)}, ['camera_id','kind','members','media_base64']),
 'ScheduleInput': obj({'camera_id': S('string', format='uuid'), 'timezone': S('string', description='Zona IANA'), 'hour': S('integer', minimum=0, maximum=23), 'minute': S('integer', minimum=0, maximum=59), 'weekdays': S('array', items=S('integer', minimum=0, maximum=6), minItems=1, maxItems=7), 'enabled': S('boolean'), 'overlap': S('string', enum=['skip'], default='skip')}, ['camera_id','timezone','weekdays']),
 'CredentialInput': obj({'scopes': S('array', items=S('string', enum=['read','control','events','predict','admin','*']), minItems=1), 'expires_at': S('string', format='date-time')}, ['scopes','expires_at']),
 'DestinationInput': obj({'url': S('string', format='uri'), 'token': S('string', writeOnly=True, minLength=16, maxLength=4096)}, ['url','token']),
 'Error': obj({name:S('string') for name in ['error','message','request_id']}, ['error','message','request_id']),
 'Operation': S('object', required=['id','environment','tenant_id','request_id','kind','state','resource_id','created_at'], properties={**{name:S('string') for name in ['id','environment','tenant_id','request_id','kind','resource_id','created_at']}, 'state':S('string', enum=['pending','succeeded','failed']), 'result':{}}),
}
owner = {key:S('string') for key in ['environment','tenant_id']}
member_output = {**schemas['MemberInput']['properties'],'generation':S('string',format='uuid'),'state':S('string',enum=['pending','warming','running','paused','failed','stopped'])}
schemas.update({
 'Member':obj(member_output,['member_id','model','version','config','fps','generation','state','resident_policy']),
 'Camera':obj({**owner,**{key:S('string') for key in ['id','code','name','created_at']},'enabled':S('boolean'),'revision':S('integer',minimum=1),'members':S('array',items=R('Member'))},['environment','tenant_id','id','code','name','created_at','enabled','revision','members']),
 'Execution':obj({**owner,**{key:S('string') for key in ['id','camera_id','generation','worker_id','reason','terminal','created_at','queue_deadline','finished_at','operation_id']},'revision':S('integer'),'observed_revision':S('integer'),'state':S('string',enum=['queued','preparing','running','stopping','completed','cancelled','failed','expired']),'desired':S('string',enum=['run','stop']),'kind':S('string',enum=['stream','image','clip']),'members':S('array',items=R('Member'))},['environment','tenant_id','id','camera_id','generation','revision','state','desired','kind','members','created_at','queue_deadline','operation_id']),
 'Frame':obj({'id':S('string',minLength=1,maxLength=120),'capturedAt':S('string',format='date-time'),'media_time':S('number'),'width':S('integer',minimum=1,maximum=16384),'height':S('integer',minimum=1,maximum=16384),'source_width':S('integer',minimum=1),'source_height':S('integer',minimum=1),'transform':S('array',items=S('number'),minItems=6,maxItems=6)},['id','capturedAt','media_time','width','height','source_width','source_height','transform']),
 'Detection':obj({'label':S('string'),'score':S('number',minimum=0,maximum=1),'bbox':S('array',items=S('number'),minItems=4,maxItems=4)},['label','score','bbox']),
 'Event':obj({**owner,**{key:S('string') for key in ['id','camera_id','execution_id','worker_id','generation','member_id','member_generation','model','type','state','created_at']},'sequence':S('integer',minimum=1),'revision':S('integer'),'score':S('number'),'frame':R('Frame'),'detections':S('array',items=R('Detection'),maxItems=200),'extra':S('object')},['environment','tenant_id','id','sequence','camera_id','execution_id','worker_id','generation','member_id','member_generation','model','revision','type','score','frame','detections','created_at']),
 'Quota':obj({key:S('integer',minimum=0) for key in ['cameras','executions','memory_mib','queue']},['cameras','executions','memory_mib','queue']),
 'Delivery':obj({**{key:S('string') for key in ['id','event_id','next_at']},'state':S('string',enum=['pending','delivered','dead']),'attempts':S('integer'),'last_status':S('integer'),'destination_revision':S('integer')},['id','event_id','state','attempts','next_at','last_status','destination_revision']),
 'Schedule':obj({**owner,**schemas['ScheduleInput']['properties'],'id':S('string'),'revision':S('integer')},['environment','tenant_id','id','revision','camera_id','timezone','hour','minute','weekdays','enabled','overlap']),
 'Model':S('object',required=['name','version','task','capabilities','event_types','config_schema','target_fps','runtime','available'],properties={**{key:S('string') for key in ['name','version','task','runtime','reason']},'capabilities':S('array',items=S('string',enum=['stream','image','clip'])),'event_types':S('array',items=S('string')),'config_schema':S('object'),'target_fps':S('number'),'available':S('boolean'),'profile':S('object')}),
 'EventPage':obj({'events':S('array',items=R('Event')),'cursor':S('string'),'retention':S('string')},['events','cursor','retention']),
})
outputs={'models':S('array',items=R('Model')),'model':R('Model'),'cameras':S('array',items=R('Camera')),'camera':R('Camera'),'execution':R('Execution'),'operation':R('Operation'),'listEvents':R('EventPage'),'deliveries':S('array',items=R('Delivery')),'schedules':S('array',items=R('Schedule'))}
inputs = dict(createCamera='CameraInput', patchCamera='CameraPatch', composition='CompositionInput', addMember='MemberInput', patchMember='MemberPatch', start='ExecutionInput', prediction='PredictionInput', createSchedule='ScheduleInput', patchSchedule='ScheduleInput', issueKey='CredentialInput', destination='DestinationInput')
revision = {'patchCamera','composition','addMember','patchMember','deleteMember','disconnect','reactivate','patchSchedule','deleteSchedule'}
errors = {400:'Petición inválida',401:'Credencial inválida',403:'Scope insuficiente',404:'Recurso ajeno o desconocido',409:'Conflicto/capacidad/perfil',410:'Cursor vencido',412:'Revisión obsoleta',413:'Medio demasiado grande',422:'Configuración inválida',428:'If-Match requerido',500:'Error interno'}
paths = {}
routes = re.findall(r'mux.Handle\("([A-Z]+) ([^\"]+)", s.secure\("([^\"]+)", s\.(\w+)\)\)', (ROOT/'internal/api/server.go').read_text())
for method,path,scope,handler in routes:
    parameters = [{'in':'path','name':name,'required':True,'schema':S('string')} for name in re.findall(r'\{(\w+)\}',path)]
    if method in ['POST','PUT','PATCH','DELETE']:
        parameters.append({'in':'header','name':'Idempotency-Key','required':True,'schema':S('string',minLength=8,maxLength=120)})
    if handler in revision:
        parameters.append({'in':'header','name':'If-Match','required':True,'schema':S('string',description='Revisión entera; admite comillas')})
    responses = {str(code): {'description':message, 'content':{'application/json':{'schema':R('Error')}}} for code,message in errors.items()}
    responses.update({'200':{'description':'Recurso scoped o resultado de operación'}, '202':{'description':'Intención durable; consultar operación y ejecución'}})
    operation = dict(operationId=handler, summary=handler, security=[{'bearerAuth':[]}], parameters=parameters, responses=responses)
    operation['x-required-scope'] = scope
    if handler in outputs:
        responses['200']['content']={'application/json':{'schema':outputs[handler]}}
    elif method in ['POST','PUT','PATCH','DELETE'] and handler!='issueKey':
        for status in ['200','202']:
            responses[status]['content']={'application/json':{'schema':R('Operation')}}
    if handler in inputs:
        operation['requestBody'] = {'required':True,'content':{'application/json':{'schema':R(inputs[handler])}}}
    if handler=='issueKey':
        responses['201']={'description':'operation y key una sola vez; repetición devuelve key vacío, sin persistir el secreto'}
    if handler in ['listEvents','live']:
        parameters.append({'in':'query','name':'cursor','schema':S('string')})
    if handler=='listEvents':
        parameters += [{'in':'query','name':'execution_id','schema':S('string')},{'in':'query','name':'limit','schema':S('integer',minimum=1,maximum=200,default=100)}]
    if handler=='live':
        parameters.append({'in':'header','name':'Last-Event-ID','schema':S('string')})
        responses['200']={'description':'Replay→vivo por secuencia confirmada, revalidación de credencial cada bloque','content':{'text/event-stream':{'schema':S('string')}}}
    paths.setdefault(path,{})[method.lower()] = operation
for path in ['/healthz','/readyz']:
    paths[path]={'get':{'operationId':path[1:],'summary':path[1:],'security':[],'responses':{'200':{'description':'Salud del control/almacén'},'503':{'description':'Almacén no disponible'}}}}
api={'openapi':'3.1.0','info':{'title':'Kinich Go / Python','version':'0.1.0','description':'El entorno y tenant se derivan de la credencial; no de body, query ni Host. Fuente privada y UUID estable. IPC NDJSON v1 por pipes.'},'servers':[{'url':'http://127.0.0.1:8090'}],'security':[{'bearerAuth':[]}],'paths':paths,'components':{'securitySchemes':{'bearerAuth':{'type':'http','scheme':'bearer','bearerFormat':'API key kg_ scoped'}},'schemas':schemas}}
(ROOT/'docs/openapi.json').write_text(json.dumps(api,ensure_ascii=False,indent=2)+'\n')
print('OpenAPI:',len(paths),'rutas,',len(routes),'operaciones autenticadas')
