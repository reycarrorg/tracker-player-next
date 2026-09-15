"""Deterministic local transport fixtures; production still requires public HTTPS."""
import http.client
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import download_providers
import transport
from test_engine import wav_bytes


PAYLOAD=wav_bytes()


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    resume_requests=0
    def log_message(self,*args):pass
    def send_payload(self,payload=PAYLOAD,status=200,content_type='audio/wav',extra=None):
        self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(payload)))
        for key,value in (extra or {}).items():self.send_header(key,value)
        self.end_headers();self.wfile.write(payload)
    def do_GET(self):
        if self.path=='/redirect':
            self.send_response(302);self.send_header('Location','/file');self.send_header('Content-Length','0');self.end_headers();return
        if self.path=='/file':self.send_payload();return
        if self.path=='/mime':self.send_payload(content_type='application/octet-stream');return
        if self.path=='/login':self.send_payload(b'<html><input type="password">Sign in</html>',content_type='text/html');return
        if self.path=='/expired':self.send_payload(b'expired',status=410,content_type='text/plain');return
        if self.path=='/executable':self.send_payload(b'\xcf\xfa\xed\xfe'+b'0'*9000,content_type='application/octet-stream');return
        if self.path=='/resume':
            type(self).resume_requests+=1
            requested=self.headers.get('Range','')
            if requested:
                offset=int(requested.split('=')[1].split('-')[0]);rest=PAYLOAD[offset:]
                self.send_payload(rest,status=206,extra={'Content-Range':f'bytes {offset}-{len(PAYLOAD)-1}/{len(PAYLOAD)}'});return
            halfway=len(PAYLOAD)//2
            self.send_response(200);self.send_header('Content-Type','audio/wav');self.send_header('Content-Length',str(len(PAYLOAD)));self.end_headers()
            self.wfile.write(PAYLOAD[:halfway]);self.wfile.flush();self.connection.shutdown(1);return
        self.send_payload(b'missing',status=404,content_type='text/plain')


class LocalOpener:
    def __call__(self,url,headers=None):
        for _ in range(5):
            parsed=urlsplit(url);conn=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=3)
            conn.request('GET',(parsed.path or '/')+('?' +parsed.query if parsed.query else ''),headers=headers or {})
            response=conn.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location');conn.close();url=urljoin(url,location);continue
            if response.status not in (200,206):
                status=response.status;conn.close()
                raise transport.AccessError(transport.classify_http(status),f'Fixture returned HTTP {status}.')
            return conn,response,url
        raise transport.MediaError('Too many redirects.')

class QuietServer(ThreadingHTTPServer):
    def handle_error(self,request,client_address):pass


class ProviderFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=QuietServer(('127.0.0.1',0),FixtureHandler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        # The injected fixture opener speaks local HTTP, while the URL remains
        # HTTPS so the production provider boundary is exercised unchanged.
        cls.base=f'https://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'asset.part';self.opener=LocalOpener()
    def tearDown(self):self.tmp.cleanup()
    def download(self,route,**kwargs):
        return download_providers.download(self.base+route,self.path,16*1048576,open_response=self.opener,**kwargs)
    def test_redirect_and_content_sniff(self):
        result=self.download('/redirect');self.assertEqual(result['kind'],'audio');self.assertEqual(result['provider'],'public_https');self.assertEqual(self.path.read_bytes(),PAYLOAD)
    def test_mime_is_advisory_and_bytes_are_validated(self):
        self.assertEqual(self.download('/mime')['mime'],'audio/wav')
    def test_interrupted_transfer_resumes_with_range(self):
        FixtureHandler.resume_requests=0
        result=self.download('/resume');self.assertTrue(result['resumed']);self.assertEqual(result['attempts'],2);self.assertEqual(self.path.read_bytes(),PAYLOAD);self.assertEqual(FixtureHandler.resume_requests,2)
    def test_expired_and_login_are_classified(self):
        with self.assertRaises(transport.AccessError) as expired:self.download('/expired')
        self.assertEqual(expired.exception.code,'broken_source')
        with self.assertRaises(transport.AccessError) as login:self.download('/login')
        self.assertEqual(login.exception.code,'authentication_required')
    def test_cancellation_keeps_no_verified_result(self):
        seen=[False]
        def progress(done,total):seen[0]=True
        with self.assertRaises(transport.AccessError) as stopped:self.download('/file',progress=progress,cancel=lambda:seen[0])
        self.assertEqual(stopped.exception.code,'cancelled');self.assertTrue(self.path.exists())
    def test_executable_payload_is_rejected(self):
        with self.assertRaises(transport.MediaError):self.download('/executable')


if __name__=='__main__':unittest.main()
