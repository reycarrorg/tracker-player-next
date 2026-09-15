"""Full-era orchestration, source recovery, and strict artwork ownership.

Browser cookies remain in WebKit. This module receives only a user-confirmed
local file after an authenticated browser download finishes.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import threading
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit
import transport
import download_providers
from delivery_metadata import public_record, destination, write_tags, MetadataError


def classification(error):
    if isinstance(error,(TimeoutError,ConnectionError,socket.gaierror,OSError)):return 'network_failure'
    code=getattr(error,'code','access_unavailable' if isinstance(error,transport.MediaError) else 'local_failure')
    return code if code in ('authentication_required','access_unavailable','network_failure','broken_source','no_source','metadata_failed','cancelled') else 'local_failure'


def group_key(row):
    return json.dumps([str(row.get('tabId') or row['workbook']),str(row['era'])],ensure_ascii=False,separators=(',',':'))


class DeliveryMixin:
    def source_recovery(self,p):
        from engine import Problem
        with self.transaction():
            job=self.db.execute('SELECT * FROM jobs WHERE id=?',(p['id'],)).fetchone()
            if not job:raise Problem('unknown_job','Unknown transfer.')
            request=self.pref('delivery:'+job['id'],{})
            row=self.row(job['row_id']);links=row['links'];index=request.get('sourceIndex',0)
            if not isinstance(index,int) or index<0 or index>=len(links):index=0
            url=links[index] if links else ''
            parsed=urlsplit(url)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
                raise Problem('invalid_source','No safe provider page is available.')
            if job['state'] not in ('awaiting_access','failed','placeholder'):raise Problem('stale_job','This transfer no longer needs source recovery.')
            self.job_update(job['id'],state='awaiting_access',code='authentication_required' if request.get('classification')=='authentication_required' else 'access_unavailable')
        return {'url':url,'sessionShared':False,'sessionOwner':'WebKit','message':'Sign in inside Tracker Player, then use the provider’s download control. WebKit keeps the site session for quicker later downloads. Passwords, cookies, and tokens never enter the download engine; only the completed file is handed back for validation and metadata.'}

    def mark_unresolved(self,p):
        from engine import Problem
        with self.transaction():
            job=self.db.execute('SELECT * FROM jobs WHERE id=?',(p['id'],)).fetchone()
            if not job or job['state'] not in ('awaiting_access','failed','interrupted','cancelled','placeholder'):raise Problem('stale_job','Only unresolved transfers can create a placeholder.')
            row=self.row(job['row_id'])
            reason=self.pref('delivery:'+job['id'],{}).get('reason') or job['error'] or 'User marked this requested file unresolved.'
            return self.placeholder(row,job['id'],job['code'] or 'access_unavailable',reason)

    def placeholder(self,row,jid,code,reason):
        from engine import contained,safe_name,stamp,encode
        # A marker never claims that a real, verified recording is absent.
        if self.local(row['id']):
            self.job_update(jid,state='skipped',error='Verified audio already exists; no placeholder created.')
            return {'ok':True}
        folder=contained(self.root/'Downloads',Path('Suzy Tracker')/safe_name(row['workbook'])/safe_name(row['era']))
        folder.mkdir(parents=True,exist_ok=True)
        payload=('TRACKER PLAYER — UNRESOLVED RECORDING\n\nClassification: '+code+'\nWhy no audio was saved: '+str(public_record(reason))+'\n\nSource row and all available metadata (URL query values and session fields redacted):\n'+json.dumps(public_record(row),ensure_ascii=False,indent=2)+'\n').encode('utf-8')
        checksum=hashlib.sha256(payload).hexdigest();target=destination(folder,row,'txt',checksum)
        if not target.exists():
            with open(target,'xb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
        self.setpref('placeholder:'+row['id'],{'path':str(target),'classification':code,'created':stamp()})
        self.job_update(jid,state='placeholder',code=code,error=public_record(reason),bytes=0)
        return {'path':str(target)}

    def download_all(self,p):
        from engine import Problem
        if not p.get('workbook') or not p.get('era'):raise Problem('era_required','Choose a worksheet and era first.')
        # Do not apply the visible search, media filter, or page limit to Download All.
        ids=self.ids({'workbook':p['workbook'],'era':p['era']})
        if not ids:raise Problem('empty_era','This era has no requested rows.')
        return self.enqueue({'ids':ids,'allEra':True,'workbook':p['workbook'],'era':p['era']})

    def retry_batch(self,p):
        from engine import Problem
        with self.transaction():
            jobs=self.db.execute("SELECT id FROM jobs WHERE batch=? AND state IN ('failed','cancelled','interrupted','placeholder','awaiting_access','metadata_failed')",(p['batch'],)).fetchall()
            if not jobs:raise Problem('nothing_to_retry','No stopped or unresolved rows in this era run.')
            for job in jobs:self.retry({'id':job['id']})
        return {'count':len(jobs)}

    def retry(self,p):
        from engine import Problem
        with self.transaction():
            job=self.db.execute("SELECT * FROM jobs WHERE id=? AND state IN ('failed','cancelled','interrupted','placeholder','awaiting_access','metadata_failed')",(p['id'],)).fetchone()
            if not job:raise Problem('not_retryable','Only stopped or unresolved transfers can be retried.')
            self.cancelled.discard(job['id']);self.job_update(job['id'],state='queued',error='',code='')
            self.pool.submit(self.run_job,job['id'])
        return {'ok':True}

    def recovery_failure(self,row,job,error,source='',attempts=None):
        code=classification(error);reason=public_record(str(error))
        try:source_index=row['links'].index(source)
        except (ValueError,AttributeError):source_index=0
        self.setpref('delivery:'+job['id'],{'classification':code,'reason':reason,'sourceIndex':source_index,'attempts':public_record(attempts or [])})
        if code=='no_source':self.placeholder(row,job['id'],code,reason)
        elif code in ('authentication_required','access_unavailable'):
            self.job_update(job['id'],state='awaiting_access',code=code,error=reason+' Open the provider, attach a manually downloaded file, retry this source, or create a placeholder.')
        elif code=='broken_source':self.placeholder(row,job['id'],code,reason)
        else:self.job_update(job['id'],state='failed',code=code,error=reason)

    def request_asset(self,row,path,limit,source=None,**kwargs):
        """Serialize identical URLs; reuse payloads from completed registry entries."""
        source=source or row['links'][0]
        canonical=source
        parsed=urlsplit(source)
        if parsed.hostname in ('pillows.su','www.pillows.su'):
            canonical='pillow:'+parsed.path.rstrip('/').rsplit('/',1)[-1]
        key=hashlib.sha256(canonical.encode()).hexdigest()
        with self.lock:
            if not hasattr(self,'asset_locks'):self.asset_locks={}
            mutex=self.asset_locks.setdefault(key,threading.Lock())
        # The caller holds this lock through final installation.
        mutex.acquire()
        try:
            with self.lock:known=[dict(r) for r in self.db.execute("SELECT * FROM files WHERE state='available'")]
            for rec in known:
                record=json.loads(rec['record'])
                if record.get('requestKey')!=key or not self.local(rec['id']):continue
                if kwargs['cancel']():raise transport.AccessError('cancelled','Cancelled before reuse.')
                size=Path(rec['path']).stat().st_size
                if size>limit:raise transport.FileLimit(limit,size)
                self.disk_guard(size)
                shutil.copyfile(rec['path'],path)
                if kwargs['cancel']():raise transport.AccessError('cancelled','Cancelled during reuse.')
                info={k:record[k] for k in ('originalUrl','resolvedUrl','kind','extension','mime') if k in record}
                info.update(bytes=size,checksum=hashlib.sha256(path.read_bytes()).hexdigest(),deduplicated=True,requestKey=key)
                return info,mutex
            info=download_providers.download(source,path,limit,**kwargs);info['requestKey']=key
            return info,mutex
        except BaseException:mutex.release();raise

    def tag_download(self,path,row,info):
        from engine import digest
        if info['kind']!='audio':return info
        selection=self.delivery_art(row)
        artwork=None
        if selection.get('path'):
            artwork=Path(selection['path']).read_bytes()
        result=dict(info,sourceChecksum=info['checksum'],sourceBytes=info['bytes'],artworkAssignment=public_record({k:v for k,v in selection.items() if k!='path'}))
        try:result['tags']=write_tags(path,row,artwork)
        except Exception as e:result['metadataFailure']=public_record(str(e))
        result['checksum']=digest(path);result['bytes']=path.stat().st_size
        return result

    def attach_download(self,p):
        from engine import Problem,regular_reader,contained,safe_name,digest,encode,stamp
        # User chooses one file in NSOpenPanel and confirms exact-row provenance.
        with self.transaction():
            job=self.db.execute('SELECT * FROM jobs WHERE id=?',(p['id'],)).fetchone()
            if not job or job['state'] not in ('awaiting_access','failed','placeholder','interrupted','cancelled'):raise Problem('stale_job','This transfer no longer accepts a manual file.')
            row=self.row(job['row_id']);self.job_update(job['id'],state='running',error='Importing user-selected file.')
        path=Path(p['path']);part=self.root/'Downloads'/('.'+job['id']+'.manual.part')
        try:
            if not path.is_absolute():raise Problem('invalid_file','Choose an absolute regular file.')
            with regular_reader(path) as source:
                size=os.fstat(source.fileno()).st_size;self.disk_guard(size)
                if size>self.limits()['fileMB']*1048576 and p.get('allowLarge') is not True:raise Problem('file_limit','Manual file exceeds the current file limit; approval required.')
                with open(part,'xb') as target:shutil.copyfileobj(source,target,1048576)
            header=part.open('rb');first=header.read(8192);header.close();kind,ext,mime=transport.sniff(first)
            if kind=='audio':
                import mutagen
                audio=mutagen.File(part)
                if audio is None or audio.info.length<=0:raise Problem('invalid_file','The selected file is not valid audio.')
            info={'bytes':part.stat().st_size,'checksum':digest(part),'kind':kind,'extension':ext,'mime':mime,'manualSelection':True,'originalUrl':public_record(row['links'][0]) if row['links'] else ''}
            info=self.tag_download(part,row,info)
            folder=contained(self.root/'Downloads',Path('Suzy Tracker')/safe_name(row['workbook'])/safe_name(row['era']));folder.mkdir(parents=True,exist_ok=True)
            target=destination(folder,row,ext,info['checksum'])
            with self.transaction():
                if job['id'] in self.cancelled or self.closing:raise Problem('cancelled','Cancelled; manual original preserved.')
                if not target.exists():os.link(part,target)
                self.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?,?)',(row['id'],str(target),info['checksum'],info['bytes'],kind,'permanent','available',stamp(),stamp(),encode(dict(info,sourceSnapshot=public_record(row)))))
                self.job_update(job['id'],state='metadata_failed' if info.get('metadataFailure') else 'completed',code='metadata_failed' if info.get('metadataFailure') else 'downloadable',bytes=info['bytes'],total=info['bytes'],error=info.get('metadataFailure') or info.get('artworkAssignment',{}).get('reason',''))
            return {'path':str(target)}
        except Exception as error:
            self.recovery_failure(row,job,error);raise
        finally:
            if part.exists():part.unlink()

    def save_copy(self,p):
        """Copy a verified app-owned download to one explicit user destination."""
        from engine import Problem,regular_reader,digest
        with self.lock:
            job=self.db.execute('SELECT * FROM jobs WHERE id=?',(p.get('id',''),)).fetchone()
            if not job:raise Problem('unknown_job','Unknown transfer.')
            record=self.db.execute("SELECT * FROM files WHERE id=? AND state='available'",(job['row_id'],)).fetchone()
        if not record or not self.local(job['row_id']):raise Problem('missing_file','No verified saved file is available to copy.')
        source=Path(record['path']);target=Path(p.get('path',''))
        if not target.is_absolute() or not target.name:raise Problem('invalid_destination','Choose an absolute file destination.')
        if target.exists():raise Problem('destination_exists','That destination already exists. Choose a new name so nothing is overwritten.')
        parent=target.parent
        if not parent.is_dir() or any(item.is_symlink() for item in (parent,*parent.parents)):
            raise Problem('unsafe_path','The destination folder must exist and cannot use symbolic links.')
        created=False
        try:
            with regular_reader(source) as src:
                fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);created=True
                hasher=hashlib.sha256();copied=0
                with os.fdopen(fd,'wb') as dst:
                    for chunk in iter(lambda:src.read(1048576),b''):
                        hasher.update(chunk);copied+=len(chunk);dst.write(chunk)
                    dst.flush();os.fsync(dst.fileno())
            if copied!=record['bytes'] or hasher.hexdigest()!=record['checksum']:
                raise Problem('changed','Saved source changed while copying.')
            with regular_reader(target) as check:
                verified=hashlib.sha256()
                for chunk in iter(lambda:check.read(1048576),b''):verified.update(chunk)
            if verified.hexdigest()!=record['checksum']:raise Problem('copy_checksum','Copied bytes differ from the verified source.')
            return {'path':str(target),'checksum':record['checksum'],'bytes':record['bytes']}
        except BaseException:
            if created and target.exists() and not target.is_symlink():target.unlink()
            raise

    def copy_info(self,p):
        from engine import Problem
        with self.lock:
            job=self.db.execute('SELECT * FROM jobs WHERE id=?',(p.get('id',''),)).fetchone()
            record=self.db.execute("SELECT * FROM files WHERE id=? AND state='available'",(job['row_id'],)).fetchone() if job else None
        if not job or not record or not self.local(job['row_id']):raise Problem('missing_file','No verified saved file is available to copy.')
        return {'suggestedName':Path(record['path']).name,'bytes':record['bytes'],'checksum':record['checksum']}

    def delivery_art(self,row):
        assignments=self.pref('artAssignments',{})
        key=group_key(row);row_selection=assignments.get('row:'+row['id'])
        if not row_selection and row['id']!='__group__' and row.get('title')!=row['era']:
            candidate=self.adapter.matching_art(row['era'],row.get('title',''))
            if candidate:
                for selection in self.adapter.bundled_art.groups.values():
                    if selection.get('sourceRowId')==candidate['rowId']:
                        path=self.adapter.bundled_art.path(selection['assetId'])
                        if path and Path(path).read_bytes().startswith((b'\xff\xd8\xff',b'\x89PNG\r\n\x1a\n')):
                            return dict(selection,path=path,rowId=selection['assetId'],exportEligible=True,caption='Unique source-row artwork match',scope='row:'+row['id'])
        selected=row_selection or assignments.get('group:'+key)
        if selected:
            if not re.fullmatch(r'assigned-[a-f0-9]{64}\.(jpg|png)',selected.get('file','')):
                return {'reason':'Invalid assigned artwork path.','missing':True}
            path=self.root/'Artwork'/selected['file']
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=selected['sha256']:
                return {'reason':'Assigned artwork file is missing or changed.','missing':True}
            if not row_selection and row['workbook']=='Released' and not selected.get('official'):
                return {'reason':'Released fallback needs an explicitly identified official album cover.','missing':True}
            if not row_selection and row['workbook']!='Released':
                reused=[k for k,v in assignments.items() if k.startswith('group:') and k!='group:'+key and v['sha256']==selected['sha256'] and not v.get('released')]
                if reused:return {'reason':'Duplicate artwork across non-Released worksheet-era groups.','missing':True}
            return dict(selected,path=str(path),rowId='assigned-'+selected['sha256'],exportEligible=True,bundled=False)
        # Only provider-backed album metadata can automatically qualify as official.
        selection=self.adapter.bundled_art.for_group(row['workbook'],row['era'])
        if row['workbook']=='Released' and selection and selection.get('association')=='album_metadata':
            path=self.adapter.bundled_art.path(selection['assetId'])
            return dict(selection,path=path,exportEligible=True,official=True)
        return {'missing':True,'group':key,'reason':'Missing official Released cover assignment.' if row['workbook']=='Released' else 'Missing unique cover for this worksheet-era group. No other group’s image was reused.'}

    def assign_art(self,p):
        from engine import Problem,regular_reader
        row=self.row(p['rowId']);path=Path(p['path'])
        with regular_reader(path) as f:data=f.read(8*1048576+1)
        if len(data)>8*1048576 or not data.startswith((b'\xff\xd8\xff',b'\x89PNG\r\n\x1a\n')):raise Problem('invalid_art','Choose a JPEG or PNG under 0.008 GB.')
        dimensions=subprocess.run(['/usr/bin/sips','-g','pixelWidth','-g','pixelHeight',str(path)],capture_output=True,text=True,timeout=15)
        sizes=re.findall(r'pixel(?:Width|Height): (\d+)',dimensions.stdout)
        if dimensions.returncode or len(sizes)!=2 or any(not 0<int(n)<=20000 for n in sizes):raise Problem('invalid_art','The selected artwork could not be decoded as a bounded image.')
        checksum=hashlib.sha256(data).hexdigest();key='row:'+row['id'] if p.get('scope')=='row' else 'group:'+group_key(row)
        with self.transaction():
            mapping=self.pref('artAssignments',{})
            if key.startswith('group:') and row['workbook']!='Released' and any(k.startswith('group:') and k!=key and not v.get('released') and v['sha256']==checksum for k,v in mapping.items()):raise Problem('duplicate_art','This cover already belongs to another non-Released worksheet-era group. Choose a different cover.')
            if key.startswith('group:') and row['workbook']=='Released' and p.get('official') is not True:raise Problem('official_required','Released fallback requires an official album cover identified by the user.')
            name='assigned-'+checksum+('.png' if data.startswith(b'\x89PNG') else '.jpg');target=self.root/'Artwork'/name
            if target.is_symlink():raise Problem('invalid_art','Unsafe artwork destination.')
            if not target.exists():
                with open(target,'xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            mapping[key]={'file':name,'sha256':checksum,'official':p.get('official') is True,'released':row['workbook']=='Released','caption':'User-assigned song artwork' if key.startswith('row:') else 'User-identified official album cover' if row['workbook']=='Released' else 'Unique worksheet-era cover','sourceUrl':'','scope':key}
            self.setpref('artAssignments',mapping)
        return {'ok':True}

    def artwork_report(self,p):
        groups={group_key(r):r for r in self.catalog['rows']}
        missing=[{'worksheet':r['workbook'],'era':r['era'],'key':k,'reason':a['reason']} for k,r in groups.items() for a in [self.delivery_art(dict(r,id='__group__'))] if a.get('missing')]
        return {'groups':len(groups),'missing':missing,'assigned':len(groups)-len(missing)}
