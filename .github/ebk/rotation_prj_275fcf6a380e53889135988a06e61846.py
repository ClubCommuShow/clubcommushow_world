"""Publish only the rotation table and its explicitly bound venue copies."""
import base64, datetime, hashlib, hmac, json, os, re, secrets, sys, time
from urllib import request, parse, error

class GoogleRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u=parse.urlparse(newurl)
        if u.scheme!='https' or u.hostname!='script.googleusercontent.com' or u.username:
            raise ValueError('Unexpected relay redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def validate(table):
    if not isinstance(table,dict) or table.get('schemaVersion')!=1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',table.get('revision','')):raise ValueError('Invalid rotation document')
    days=table.get('days'); seen=set(); rows=0
    if not isinstance(days,list) or len(days)>31:raise ValueError('Invalid dates')
    def text(v,n,empty=False):return isinstance(v,str) and len(v.encode("utf-16-le"))//2<=n and (empty or v.strip()) and not re.search(r'[\x00-\x1f\x7f]',v)
    for d in days:
        date=d['date']; parsed=datetime.date.fromisoformat(date)
        if parsed.isoformat()!=date or not 2000<=parsed.year<=2199 or date in seen:raise ValueError('Invalid date')
        seen.add(date); sections=d['sections']; ids=set(); previous=''
        if not isinstance(sections,list) or len(sections)>16:raise ValueError('Too many timings')
        for s in sections:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',s['id']) or s['id'] in ids or not text(s['title'],48):raise ValueError('Invalid timing')
            ids.add(s['id']); at=s.get('time','')
            if at:
                if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',at) or at<=previous:raise ValueError('Invalid time order')
                previous=at
            assignments=s['assignments']
            if not isinstance(assignments,list) or len(assignments)>16:raise ValueError('Too many assignments')
            people=set(); places={}
            for a in assignments:
                rows+=1
                if a.get('personalGroup','') not in ('','staff-guidance') or rows>512 or not text(a.get('groupLabel',''),48,True) or not text(a['place'],48,True) or type(a['applyToService']) is not bool:raise ValueError('Invalid assignment')
                members=a['members']
                if not isinstance(members,list) or not 1<=len(members)<=4:raise ValueError('Invalid members')
                for m in members:
                    if not text(m['name'],48) or not text(m.get('vrcName',''),64,not a['applyToService']):raise ValueError('Invalid member')
                    if a['applyToService']:
                        if not a['place'] or m['vrcName'] in people:raise ValueError('Duplicate assignee')
                        people.add(m['vrcName']); places[a['place']]=places.get(a['place'],0)+1
                        if places[a['place']]>4:raise ValueError('Too many assignees')
    raw=(json.dumps(table,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8')
    if len(raw)>262144:raise ValueError('Rotation document too large')
    return raw

def relay_export(credential):
    u=parse.urlparse(credential['endpoint'])
    if u.scheme!='https' or u.netloc!='script.google.com' or not re.fullmatch(r'/macros/s/[A-Za-z0-9_-]+/exec',u.path) or u.query or u.fragment:raise ValueError('Invalid relay URL')
    if credential['role']!='publisher' or credential['capabilities']!=['rotation.export']:raise ValueError('Publisher credential required')
    call={k:credential.get(k,'') for k in ['deploymentId','eventId','worldId','operatorId']}
    call.update(schema=1,action='rotation.export')
    data=json.dumps(call,ensure_ascii=False,separators=(',',':')); stamp=int(time.time()); nonce=secrets.token_hex(16)
    canonical=f"{credential['keyId']}\n{stamp}\n{nonce}\n{data}"
    envelope=dict(keyId=credential['keyId'],timestamp=stamp,nonce=nonce,data=data,signature=hmac.new(credential['secret'].encode(),canonical.encode(),hashlib.sha256).hexdigest())
    req=request.Request(credential['endpoint'],data=json.dumps(envelope).encode(),headers={'Content-Type':'application/json'})
    with request.build_opener(GoogleRedirect()).open(req,timeout=45) as response:raw=response.read(1048577)
    if len(raw)>1048576:raise ValueError('Relay response too large')
    result=json.loads(raw)
    if result.get('ok') is not True:raise ValueError('Relay rejected rotation export; inspect OPS sync status')
    return validate(result['result'])

def publish(raw,repo,branch,root,token,refresh=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) or not re.fullmatch(r'docs/EBK/projects/prj_[A-Za-z0-9_-]+',root):raise ValueError('Invalid project destination')
    api='https://api.github.com/repos/'+repo
    def gh(method,path,body=None):
        req=request.Request(api+path,method=method,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'})
        with request.urlopen(req,timeout=45) as r:return json.load(r)
    def blob(sha):
        b=gh('GET','/git/blobs/'+sha)
        if b.get('encoding')!='base64' or b.get('size',0)>262144:raise ValueError('Invalid GitHub blob')
        return base64.b64decode(b['content'])
    for attempt in range(3):
        head=gh('GET','/git/ref/heads/'+parse.quote(branch,safe=''))['object']['sha']
        tree=gh('GET','/git/commits/'+head)['tree']['sha']; listing=gh('GET','/git/trees/'+tree+'?recursive=1')
        if listing.get('truncated'):raise ValueError('Repository tree truncated')
        if refresh is not None:
            latest=refresh()
            if latest!=raw:raw=latest
        entries={e['path']:e for e in listing['tree']}; destinations=[root+'/data/rotation_table.json']
        for p,e in entries.items():
            if re.fullmatch(r'docs/EBK/venues/[^/]+/data/active_project.json',p):
                if e['type']!='blob' or e['mode']!='100644':raise ValueError('Invalid venue binding')
                binding=json.loads(blob(e['sha']))
                if binding.get('projectRoot','').strip('/')==root:destinations.append(p.rsplit('/',1)[0]+'/rotation_table.json')
        sha=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest(); changes=[]
        for p in destinations:
            for n in range(1,len(p.split('/'))):
                parent='/'.join(p.split('/')[:n])
                if parent in entries and entries[parent]['type']!='tree':raise ValueError('Invalid destination parent')
            e=entries.get(p)
            if e and (e['type']!='blob' or e['mode']!='100644'):raise ValueError('Invalid destination')
            if not e or e['sha']!=sha:changes.append(dict(path=p,mode='100644',type='blob',sha=sha))
        if not changes:return False
        gh('POST','/git/blobs',dict(content=base64.b64encode(raw).decode(),encoding='base64'))
        newtree=gh('POST','/git/trees',dict(base_tree=tree,tree=changes))['sha']
        commit=gh('POST','/git/commits',dict(message='Update EBK rotation table from private spreadsheet',tree=newtree,parents=[head]))['sha']
        try:
            gh('PATCH','/git/refs/heads/'+parse.quote(branch,safe=''),dict(sha=commit,force=False));return True
        except error.HTTPError as e:
            if e.code not in (409,422) or attempt==2:raise
    raise ValueError('Concurrent update')

if __name__=='__main__':
    try:
        credential=json.loads(os.environ['EBK_ROTATION_RELAY'])
        raw=relay_export(credential)
        changed=publish(raw,os.environ['GITHUB_REPOSITORY'],os.environ['EBK_BRANCH'],os.environ['EBK_ROOT'],os.environ['GH_TOKEN'],lambda:relay_export(credential))
        with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output:output.write('changed='+str(changed).lower()+'\n')
        print('Rotation table updated' if changed else 'Rotation table unchanged')
    except Exception as exc:
        # Never echo credentials or HTTP response bodies into an Actions log.
        print('Rotation publication failed: '+type(exc).__name__,file=sys.stderr);sys.exit(1)
