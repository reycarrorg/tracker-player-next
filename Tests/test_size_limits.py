"""Small offline fixtures for per-file approval and byte-budget isolation."""
import io
import json
import unittest
from unittest.mock import patch
import engine
import transport
from test_engine import EngineFixtures, wav_bytes

class SizeApprovalTests(unittest.TestCase):
    setUp=EngineFixtures.setUp
    tearDown=EngineFixtures.tearDown
    job=EngineFixtures.job
    fake_download=EngineFixtures.fake_download

    def waiting_job(self,rid='a',total=1368194348):
        jid=self.e.enqueue({'ids':[rid]})['jobs'][0]
        with patch('transport.download',side_effect=transport.FileLimit(128*engine.MIB,total)):
            self.e.run_job(jid)
        self.assertEqual(self.job(jid)['state'],'awaiting_approval')
        return jid

    def test_declared_oversize_stops_before_body(self):
        class Response:
            def getheader(self,k,default=None):return {'Content-Length':'1368194348','Content-Type':'video/mp4'}.get(k,default)
            def read(self,n):raise AssertionError('Oversized body read before approval')
        with patch('transport.response',return_value=(unittest.mock.Mock(),Response(),'https://example.com/a')):
            with self.assertRaises(transport.FileLimit) as caught:transport.download('https://example.com/a',self.base/'media',128*engine.MIB)
        self.assertEqual(caught.exception.requested,1368194348)
        self.assertIn('1.368 GB',str(caught.exception));self.assertNotIn('1,368,194,348',str(caught.exception))
        self.assertFalse((self.base/'media').exists())

    def test_no_declines_without_download_or_global_change(self):
        before=self.e.limits();jid=self.waiting_job();count=len(self.e.pool.calls)
        self.e.size_decision({'id':jid,'allow':False})
        self.assertEqual(self.job(jid)['state'],'cancelled');self.assertEqual(self.e.limits(),before)
        self.assertEqual(len(self.e.pool.calls),count);self.assertIsNone(self.e.local('a'))
        with self.assertRaises(engine.Problem):self.e.size_decision({'id':jid,'allow':True})

    def test_yes_is_exact_job_only_and_does_not_raise_other_budget(self):
        before=self.e.limits();jid=self.waiting_job();self.e.size_decision({'id':jid,'allow':True})
        seen=[]
        def download(url,path,limit,**kw):seen.append(limit);return self.fake_download(url,path,limit,**kw)
        with patch('transport.download',side_effect=download):self.e.run_job(jid)
        self.assertEqual(seen,[1368194348]);self.assertEqual(self.job(jid)['state'],'completed');self.assertEqual(self.e.limits(),before)
        budget=self.e.pref('batch:'+self.job(jid)['batch']);self.assertEqual(budget['limit'],512*engine.MIB)
        self.assertEqual(budget['approvedReceived'],len(wav_bytes()));self.assertEqual(budget['fileLimits'],{jid:1368194348})
        other=self.e.enqueue({'ids':['b']})['jobs'][0]
        with patch('transport.download',side_effect=download):self.e.run_job(other)
        self.assertEqual(seen[-1],128*engine.MIB)
        with self.assertRaises(engine.Problem):self.e.size_decision({'id':jid,'allow':True})

    def test_preview_token_bound_to_row_and_one_use(self):
        with patch('transport.download',side_effect=transport.FileLimit(64*engine.MIB,1368194348)):
            request=self.e.prepare({'id':'a'})['approval']
        with self.assertRaises(engine.Problem):self.e.prepare({'id':'b','sizeToken':request['token']})
        with patch('transport.download',side_effect=transport.FileLimit(64*engine.MIB,1368194348)):
            request=self.e.prepare({'id':'a'})['approval']
        before=self.e.limits()
        with patch('transport.download',side_effect=self.fake_download) as download:
            result=self.e.prepare({'id':'a','sizeToken':request['token']})
        self.assertEqual(download.call_args.args[2],1368194348);self.assertEqual(result['mode'],'temporary');self.assertEqual(self.e.limits(),before)
        with self.assertRaises(engine.Problem):self.e.prepare({'id':'a','sizeToken':request['token']})

    def test_discarded_preview_never_downloads(self):
        with patch('transport.download',side_effect=transport.FileLimit(64*engine.MIB,1368194348)):
            request=self.e.prepare({'id':'a'})['approval']
        self.e.discard_size({'token':request['token']})
        with self.assertRaises(engine.Problem):self.e.prepare({'id':'a','sizeToken':request['token']})
        self.assertFalse(list((self.e.root/'Cache').iterdir()))

    def test_changed_size_prompts_again(self):
        jid=self.waiting_job(total=200*engine.MIB);self.e.size_decision({'id':jid,'allow':True})
        with patch('transport.download',side_effect=transport.FileLimit(200*engine.MIB,300*engine.MIB)):
            self.e.run_job(jid)
        self.assertEqual(self.job(jid)['state'],'awaiting_approval')
        self.assertEqual(self.e.activity({})['jobs'][0]['approval']['requestedBytes'],300*engine.MIB)

    def test_unknown_length_remains_bounded(self):
        class Response:
            def __init__(self):self.data=io.BytesIO(wav_bytes())
            def getheader(self,k,default=None):return {'Content-Type':'audio/wav'}.get(k,default)
            def read(self,n):return self.data.read(n)
        with patch('transport.response',return_value=(unittest.mock.Mock(),Response(),'https://example.com/a')):
            with self.assertRaises(transport.FileLimit) as caught:transport.download('https://example.com/a',self.base/'unknown',1024)
        self.assertIsNone(caught.exception.total);self.assertEqual(caught.exception.requested,2048)
        self.assertEqual((self.base/'unknown').stat().st_size,1024)

    def test_cancel_and_restart_never_imply_yes(self):
        jid=self.waiting_job();self.e.cancel({});self.assertEqual(self.job(jid)['state'],'cancelled')
        jid=self.waiting_job();root=self.e.root;self.e.close();self.e=None
        self.e=engine.Engine(root,self.snap)
        self.assertIn(self.job(jid)['state'],('cancelled','interrupted'))
        with self.assertRaises(engine.Problem):self.e.size_decision({'id':jid,'allow':True})

    def test_low_disk_keeps_request_pending(self):
        jid=self.waiting_job()
        with patch.object(self.e,'disk_guard',side_effect=engine.Problem('disk_reserve','Insufficient space')):
            with self.assertRaises(engine.Problem):self.e.size_decision({'id':jid,'allow':True})
        self.assertEqual(self.job(jid)['state'],'awaiting_approval')

if __name__=='__main__':unittest.main()
