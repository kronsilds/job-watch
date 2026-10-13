"""Email provider ports. Real adapters only prepare OAuth authorization URLs; send remains explicit."""
from dataclasses import dataclass
from urllib.parse import urlencode
import os, uuid

@dataclass
class PreparedMessage:
    provider: str; provider_message_id: str; to: str; subject: str; body: str; attachment_path: str|None

class EmailProvider:
    name='base'
    def authorization_url(self, state: str) -> str: raise NotImplementedError
    def send(self, *, to, subject, body, attachment_path, idempotency_key) -> PreparedMessage: raise NotImplementedError

class MockEmailProvider(EmailProvider):
    name='mock'
    def authorization_url(self, state): return f'mock://oauth?{urlencode({"state":state})}'
    def send(self, **kw):
        return PreparedMessage(self.name, 'mock-'+uuid.uuid4().hex, kw['to'], kw['subject'], kw['body'], kw.get('attachment_path'))

class OAuthEmailProvider(EmailProvider):
    provider_key=''
    authorize_endpoint=''
    def authorization_url(self, state):
        client_id=os.getenv(self.client_id_env)
        redirect=os.getenv('OAUTH_REDIRECT_URI','http://127.0.0.1:8000/api/oauth/callback')
        if not client_id: raise RuntimeError(f'{self.client_id_env} absent; no credentials configured')
        return self.authorize_endpoint+'?'+urlencode({'client_id':client_id,'redirect_uri':redirect,'response_type':'code','scope':self.scope,'state':state})
    def send(self, **kw): raise RuntimeError('OAuth token exchange/send is intentionally not implemented in local mode; inject a vetted adapter')
class GmailProvider(OAuthEmailProvider):
    name='gmail'; client_id_env='JOBWATCH_GMAIL_CLIENT_ID'; scope='https://www.googleapis.com/auth/gmail.send'; authorize_endpoint='https://accounts.google.com/o/oauth2/v2/auth'
class OutlookProvider(OAuthEmailProvider):
    name='outlook'; client_id_env='JOBWATCH_OUTLOOK_CLIENT_ID'; scope='https://graph.microsoft.com/Mail.Send'; authorize_endpoint='https://login.microsoftonline.com/common/oauth2/v2.0/authorize'

def provider_for(name):
    return {'mock':MockEmailProvider(),'gmail':GmailProvider(),'outlook':OutlookProvider()}.get(name, None)
