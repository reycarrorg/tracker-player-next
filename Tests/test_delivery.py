"""Credential-free fixtures for delivery orchestration and source recovery."""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import engine
import transport
import delivery_metadata as metadata
from test_engine import row,snapshot,wav_bytes,DeferredPool

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name).resolve()
        self.rows=[row('a',fields={'Artist':'Real Artist','BPM':'120','Year':'2024','Notes':'Faithful row comment','Available Length':'Snippet'}),row('b',links=['https://example.com/a']),row('none',links=[],eligible=False),row('auth',links=['https://example.com/sign-in'])]
        self.e=engine.Engine(self.base/'library',snapshot(self.base/'catalog.json',self.rows));self.e.pool.shutdown(wait=True);self.e.pool=DeferredPool();self.e.disk_guard=lambda n:None
    def tearDown(self):self.e.close();self.tmp.cleanup()
    def job(self,jid):return dict(self.e.db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
    def fake(self,url,path,limit,consume=lambda n:None,**kwargs):
        data=wav_bytes();consume(len(data));Path(path).write_bytes(data)
        return dict(bytes=len(data),checksum=engine.digest(path),kind='audio',extension='wav',mime='audio/wav',originalUrl=url,resolvedUrl=url)
    def run_pending(self):
        while self.e.pool.calls:
            fn,args=self.e.pool.calls.pop(0);fn(*args)
    def test_classification(self):
        self.assertEqual(transport.classify_http(401),'authentication_required')
        self.assertEqual(transport.classify_http(403),'access_unavailable')
        self.assertEqual(transport.classify_http(404),'broken_source')
        self.assertEqual(transport.classify_http(503),'network_failure')
        self.assertEqual(transport.classify_page(b'<input type="password">'),'authentication_required')
        self.assertEqual(transport.classify_page(b'<html>temporarily unavailable</html>'),'access_unavailable')
    def test_easy_attached_source_is_tried_before_login_source(self):
        soundcloud='https://soundcloud.com/artist/song';pillow='https://pillows.su/f/'+'a'*32
        chosen=[]
        def fetch(url,*args,**kwargs):
            chosen.append(url)
            if url==soundcloud:raise transport.AccessError('authentication_required','Sign in.')
            return self.fake(url,*args,**kwargs)
        linked=row('a',links=[soundcloud,pillow])
        with patch.object(self.e,'row',return_value=linked),patch('transport.download',side_effect=fetch):
            job=self.e.enqueue({'ids':['a']})['jobs'][0];self.e.run_job(job)
        self.assertEqual(chosen,[pillow]);self.assertEqual(self.job(job)['state'],'completed')
    def test_preview_uses_same_easy_source_order(self):
        soundcloud='https://soundcloud.com/artist/song';pillow='https://pillows.su/f/'+'b'*32
        linked=row('a',links=[soundcloud,pillow]);chosen=[]
        def fetch(url,*args,**kwargs):chosen.append(url);return self.fake(url,*args,**kwargs)
        with patch.object(self.e,'row',return_value=linked),patch('transport.download',side_effect=fetch):result=self.e.prepare({'id':'a','remote':True})
        self.assertEqual(chosen,[pillow]);self.assertEqual(result['kind'],'audio')
    def test_transport_requires_https_before_dns(self):
        with patch('transport.socket.getaddrinfo') as lookup:
            with self.assertRaises(transport.MediaError):transport.public_target('http://example.com/file.mp3')
        lookup.assert_not_called()
        with patch('transport.socket.getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443))]):
            parsed,ip=transport.public_target('https://example.com/file.mp3')
        self.assertEqual(parsed.scheme,'https');self.assertEqual(ip,'93.184.216.34')
    def test_full_era_continues_deduplicates_and_makes_zero_link_placeholder(self):
        self.e.settings({'count':1})
        def fetch(url,*args,**kwargs):
            if '/sign-in' in url:raise transport.AccessError('authentication_required','Please sign in.')
            return self.fake(url,*args,**kwargs)
        with patch('transport.download',side_effect=fetch) as net:
            result=self.e.download_all({'workbook':'Unreleased','era':'Era'});self.run_pending()
        self.assertEqual(len(result['jobs']),4);self.assertEqual(net.call_count,2)
        self.assertEqual([self.job(j)['state'] for j in result['jobs']],['completed','completed','placeholder','awaiting_access'])
        self.assertTrue(Path(self.e.pref('placeholder:none')['path']).exists())
        record=self.e.local('a');self.assertEqual(Path(record['path']).parent,self.e.root/'Downloads/Suzy Tracker/Unreleased/Era')
        self.assertEqual(Path(record['path']).name,'Song a [V1] [Snippet].wav')
        self.assertEqual(json.loads(record['record'])['tags']['album'],'Era [Unreleased]')
        batch=self.e.activity({})['batches'][0]
        self.assertEqual(batch['total'],4);self.assertEqual(batch['label'],'Unreleased / Era')
    def test_auth_open_retry_and_unresolved_same_source(self):
        j=self.e.enqueue({'ids':['auth']})['jobs'][0]
        with patch('transport.download',side_effect=transport.AccessError('authentication_required','Sign in.')):self.e.run_job(j)
        opened=self.e.source_recovery({'id':j});self.assertEqual(opened['url'],'https://example.com/sign-in');self.assertFalse(opened['sessionShared'])
        self.e.retry({'id':j})
        with patch('transport.download',side_effect=transport.AccessError('authentication_required','Still requires browser session.')):self.run_pending()
        result=self.e.mark_unresolved({'id':j});text=Path(result['path']).read_text()
        self.assertIn('authentication_required',text);self.assertIn('https://example.com/sign-in',text);self.assertIn('sourceHash',text)
        self.assertEqual(self.job(j)['state'],'placeholder')
    def test_manual_attachment_preserves_original_and_writes_tags(self):
        j=self.e.enqueue({'ids':['auth']})['jobs'][0]
        with patch('transport.download',side_effect=transport.AccessError('authentication_required','Sign in.')):self.e.run_job(j)
        original=self.base/'manual.wav';original.write_bytes(wav_bytes());before=original.read_bytes()
        result=self.e.attach_download({'id':j,'path':str(original)})
        self.assertEqual(original.read_bytes(),before);self.assertEqual(self.job(j)['state'],'completed')
        self.assertIn('[Full].wav',result['path'])
    def test_cancel_access_creates_marker_retry_batch_preserves_count(self):
        jobs=self.e.download_all({'workbook':'Unreleased','era':'Era'})
        with patch('transport.download',side_effect=transport.AccessError('authentication_required','Sign in.')):self.e.run_job(jobs['jobs'][0])
        self.e.cancel({});self.assertEqual(self.job(jobs['jobs'][0])['state'],'placeholder')
        self.e.pool.calls=[];self.e.retry_batch({'batch':jobs['batch']})
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM jobs').fetchone()[0],4)
        with patch('transport.download',side_effect=self.fake):self.run_pending()
        self.assertEqual(self.e.activity({})['batches'][0]['total'],4)
    def test_existing_valid_file_is_never_overwritten(self):
        j=self.e.enqueue({'ids':['a']})['jobs'][0]
        with patch('transport.download',side_effect=self.fake):self.e.run_job(j)
        path=Path(self.e.local('a')['path']);before=path.read_bytes()
        other=self.e.enqueue({'ids':['a']})['jobs'][0]
        with patch('transport.download',side_effect=AssertionError('unexpected fetch')):self.e.run_job(other)
        self.assertEqual(self.job(other)['state'],'skipped');self.assertEqual(path.read_bytes(),before)
        self.e.placeholder(self.rows[0],other,'no_source','fixture');self.assertEqual(self.job(other)['state'],'skipped')
    def test_network_failure_is_retryable_not_a_bad_link(self):
        j=self.e.enqueue({'ids':['a']})['jobs'][0]
        with patch('transport.download',side_effect=TimeoutError('Timeout')):self.e.run_job(j)
        self.assertEqual(self.job(j)['state'],'failed');self.assertEqual(self.job(j)['code'],'network_failure')
        self.e.retry({'id':j})
        with patch('transport.download',side_effect=self.fake):self.run_pending()
        self.assertEqual(self.job(j)['state'],'completed')
    def test_name_order_portions_featured_text_and_collision(self):
        r=row(name='Some Song (feat. Singer) [v2]',version='[v2]',fields={'Available Length':'Snippet'})
        self.assertEqual(metadata.base_name(r),'Some Song (feat. Singer) [V2] [Snippet]')
        for label in ('Full','Tagged','Snippet'):
            r['fields']['Available Length']=label;self.assertTrue(metadata.base_name(r).endswith('['+label+']'))
        r.update(name='A/B: C?   Song',version='');self.assertEqual(metadata.base_name(r),'A_B_ C_ Song [Snippet]')
        first=metadata.destination(self.base,r,'wav','abc');first.write_bytes(b'existing')
        second=metadata.destination(self.base,r,'wav','abc');self.assertNotEqual(first,second);self.assertEqual(second,metadata.destination(self.base,r,'wav','abc'))
        self.assertEqual(first.read_bytes(),b'existing')
    def test_placeholder_redacts_session_fields_and_signed_queries(self):
        r=dict(self.rows[2],links=['https://example.com/file?token=do-not-export'],fields={'Notes':'password=do-not-export','Cookie':'private','Year':'2024'})
        j=self.e.enqueue({'ids':['none']})['jobs'][0];result=self.e.placeholder(r,j,'no_source','authorization=do-not-export')
        text=Path(result['path']).read_text();self.assertNotIn('do-not-export',text);self.assertNotIn('private',text);self.assertIn('2024',text)
    def test_failure_events_and_manifest_redact_signed_queries(self):
        signed='https://example.com/file?token=do-not-export&signature=private'
        signed_row=dict(self.rows[0],links=[signed])
        with self.assertRaises(transport.AccessError),patch.object(self.e,'row',return_value=signed_row),patch('transport.download',side_effect=transport.AccessError('broken_source','Failed '+signed)):
            self.e.prepare({'id':'a'})
        event=self.e.db.execute("SELECT payload FROM events WHERE type='source_failure'").fetchone()['payload']
        self.assertNotIn('do-not-export',event);self.assertNotIn('private',event);self.assertIn('%5Bredacted%5D',event)
        with self.e.transaction():self.e.event('source_failure','a',{'source':signed,'session':'private'})
        manifest=Path(self.e.manifest({})['path']).read_text()
        self.assertNotIn('do-not-export',manifest);self.assertNotIn('private',manifest);self.assertNotIn('session',manifest)
    def test_art_precedence_and_group_uniqueness(self):
        image=self.base/'cover.png';image.write_bytes(PNG)
        self.e.assign_art({'rowId':'a','path':str(image),'scope':'group'})
        self.assertTrue(self.e.delivery_art(self.rows[0])['path'])
        changed=row('other',workbook='Other',tabId='other-sheet')
        self.assertTrue(self.e.delivery_art(changed)['missing'])
        with patch.object(self.e,'row',return_value=changed):
            with self.assertRaises(engine.Problem):self.e.assign_art({'rowId':'other','path':str(image),'scope':'group'})
        self.e.assign_art({'rowId':'b','path':str(image),'scope':'row'})
        self.assertEqual(self.e.delivery_art(self.rows[1])['scope'],'row:b')
        released=row('release',workbook='Released')
        self.assertTrue(self.e.delivery_art(released)['missing'])
        with patch.object(self.e,'row',return_value=released):
            with self.assertRaises(engine.Problem):self.e.assign_art({'rowId':'release','path':str(image),'scope':'group'})
            self.e.assign_art({'rowId':'release','path':str(image),'scope':'group','official':True})
        self.assertTrue(self.e.delivery_art(released)['official'])
    def test_tag_failure_keeps_audio_and_is_reported(self):
        j=self.e.enqueue({'ids':['a']})['jobs'][0]
        with patch('transport.download',side_effect=self.fake),patch('delivery.write_tags',side_effect=metadata.MetadataError('Readback failed')):self.e.run_job(j)
        self.assertEqual(self.job(j)['state'],'metadata_failed');self.assertTrue(self.e.local('a'))
        self.assertEqual(Path(self.e.local('a')['path']).read_bytes(),wav_bytes())

if __name__=='__main__':unittest.main()
