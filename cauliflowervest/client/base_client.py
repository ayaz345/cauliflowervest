# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base CauliflowerVestClient class."""

import httplib
import json
import logging
import ssl
import time
import urllib
import urllib2
import webbrowser



import httplib2
import oauth2client.client
import oauth2client.tools


from cauliflowervest import settings as base_settings
from cauliflowervest.client import settings
from cauliflowervest.client import util

# Prefix to prevent Cross Site Script Inclusion.
JSON_PREFIX = ")]}',\n"


class Error(Exception):
  """Class for domain specific exceptions."""


class UserAbort(Error):
  """User aborted process."""


class AuthenticationError(Error):
  """There was an error with authentication."""


class RequestError(Error):
  """There was an error interacting with the server."""


class NotFoundError(RequestError):
  """No passphrase was found."""


class MetadataError(Error):
  """There was an error with machine metadata."""




class CauliflowerVestClient(object):
  """Client to interact with the CauliflowerVest service."""

  ESCROW_PATH = None  # String path to escrow to, set by subclasses.

  # Sequence of key names of metadata to require; see GetAndValidateMetadata().
  REQUIRED_METADATA = []

  # The metadata key under which the passphrase is stored.
  PASSPHRASE_KEY = 'passphrase'

  MAX_TRIES = 5  # Number of times to try an escrow upload.
  TRY_DELAY_FACTOR = 5  # Number of seconds, (* try_num), to wait between tries.

  XSRF_PATH = '/xsrf-token/%s'

  def __init__(self, base_url, opener, headers=None):
    self._metadata = None
    self.base_url = base_url
    self.xsrf_url = util.JoinURL(base_url, self.XSRF_PATH)
    if self.ESCROW_PATH is None:
      raise ValueError('ESCROW_PATH must be set by CauliflowerVestClient subclasses.')
    self.escrow_url = util.JoinURL(base_url, self.ESCROW_PATH)
    self.opener = opener
    self.headers = headers or {}

  def _GetMetadata(self):
    """Returns a dict of key/value metadata pairs."""
    raise NotImplementedError

  def RetrieveSecret(self, target_id):
    """Fetches and returns the passphrase.

    Args:
      target_id: str, Target ID to fetch the passphrase for.
    Returns:
      str: passphrase.
    Raises:
      RequestError: there was an error downloading the passphrase.
      NotFoundError: no passphrase was found for the given target_id.
    """
    xsrf_token = self._FetchXsrfToken(base_settings.GET_PASSPHRASE_ACTION)
    url = '%s?%s' % (util.JoinURL(self.escrow_url, urllib.quote(target_id)),
                     urllib.urlencode({'xsrf-token': xsrf_token}))
    request = urllib2.Request(url)
    try:
      response = self.opener.open(request)
    except urllib2.URLError as e:# Parent of urllib2.HTTPError.
      if isinstance(e, urllib2.HTTPError):
        e.msg += f': {e.read()}'
        if e.code == httplib.NOT_FOUND:
          raise NotFoundError(f'Failed to retrieve passphrase. {e}')
      raise RequestError(f'Failed to retrieve passphrase. {e}')
    content = response.read()
    if not content.startswith(JSON_PREFIX):
      raise RequestError('Expected JSON prefix missing.')
    data = json.loads(content[len(JSON_PREFIX):])
    return data[self.PASSPHRASE_KEY]

  def GetAndValidateMetadata(self):
    """Retrieves and validates machine metadata.

    Raises:
      MetadataError: one or more of the REQUIRED_METADATA were not found.
    """
    if not self._metadata:
      self._metadata = self._GetMetadata()
    for key in self.REQUIRED_METADATA:
      if not self._metadata.get(key, None):
        raise MetadataError(f'Required metadata is not found: {key}')

  def SetOwner(self, owner):
    if not self._metadata:
      self.GetAndValidateMetadata()
    self._metadata['owner'] = owner

  def _FetchXsrfToken(self, action):
    request = urllib2.Request(self.xsrf_url % action)
    response = self._RetryRequest(request, 'Fetching XSRF token')
    return response.read()

  def _RetryRequest(self, request, description, retry_4xx=False):
    """Make the given HTTP request, retrying upon failure."""
    for k, v in self.headers.iteritems():
      request.add_header(k, v)

    for try_num in range(self.MAX_TRIES):
      try:
        return self.opener.open(request)
      except urllib2.URLError as e:  # Parent of urllib2.HTTPError.
        if isinstance(e, urllib2.HTTPError):
          e.msg += f': {e.read()}'
          # Reraise if HTTP 4xx and retry_4xx is False
          if 400 <= e.code < 500 and not retry_4xx:
            raise RequestError(f'{description} failed: {e}')
        # Otherwise retry other HTTPError and URLError failures.
        if try_num == self.MAX_TRIES - 1:
          logging.exception('%s failed permanently.', description)
          raise RequestError(f'{description} failed permanently: {e}')
        logging.warning(
            '%s failed with (%s). Retrying ...', description, e)
        time.sleep((try_num + 1) * self.TRY_DELAY_FACTOR)

  def IsKeyRotationNeeded(self, target_id, tag='default'):
    """Check whether a key rotation is required.

    Args:
      target_id: str, Target ID.
      tag: str, passphrase tag.
    Raises:
      RequestError: there was an error getting status from server.
    Returns:
      bool: True if a key rotation is required.
    """
    url = '%s?%s' % (
        util.JoinURL(
            self.base_url, '/api/v1/rekey-required/',
            self.ESCROW_PATH, target_id),
        urllib.urlencode({'tag': tag}))
    request = urllib2.Request(url)
    try:
      response = self.opener.open(request)
    except urllib2.URLError as e:# Parent of urllib2.HTTPError.
      if isinstance(e, urllib2.HTTPError):
        e.msg += f': {e.read()}'
      raise RequestError(f'Failed to get status. {e}')
    content = response.read()
    if not content.startswith(JSON_PREFIX):
      raise RequestError('Expected JSON prefix missing.')
    return json.loads(content[len(JSON_PREFIX):])

  def UploadPassphrase(self, target_id, passphrase, retry_4xx=False):
    """Uploads a target_id/passphrase pair with metadata.

    Args:
      target_id: str, Target ID.
      passphrase: str, passphrase.
      retry_4xx: bool, whether to retry when errors are in the 401-499 range.
    Raises:
      RequestError: there was an error uploading to the server.
    """
    xsrf_token = self._FetchXsrfToken(base_settings.SET_PASSPHRASE_ACTION)

    class PutRequest(urllib2.Request):

      def __init__(self, *args, **kwargs):
        kwargs.setdefault('headers', {})
        kwargs['headers']['Content-Type'] = 'application/octet-stream'
        urllib2.Request.__init__(self, *args, **kwargs)
        self._method = 'PUT'

      def get_method(self):  # pylint: disable=g-bad-name
        return 'PUT'

    if not self._metadata:
      self.GetAndValidateMetadata()
    parameters = self._metadata.copy()
    parameters['xsrf-token'] = xsrf_token
    parameters['volume_uuid'] = target_id
    url = f'{self.escrow_url}?{urllib.urlencode(parameters)}'

    request = PutRequest(url, data=passphrase)
    self._RetryRequest(request, 'Uploading passphrase', retry_4xx=retry_4xx)




def BuildOauth2Opener(credentials):
  """Produce an OAuth compatible urllib2 OpenerDirective."""
  context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
  context.options |= ssl.OP_NO_SSLv2
  context.verify_mode = ssl.CERT_REQUIRED

  ca_certs_file = settings.ROOT_CA_CERT_CHAIN_PEM_FILE_PATH
  context.load_verify_locations(ca_certs_file)

  opener = urllib2.build_opener(
      urllib2.HTTPSHandler(context=context),
      urllib2.HTTPRedirectHandler())
  h = {}
  credentials.apply(h)
  opener.addheaders = h.items()
  return opener


def GetOauthCredentials():
  """Create an OAuth2 `Credentials` object."""
  if not base_settings.OAUTH_CLIENT_ID:
    raise RuntimeError('Missing OAUTH_CLIENT_ID setting!')
  if not settings.OAUTH_CLIENT_SECRET:
    raise RuntimeError('Missing OAUTH_CLIENT_SECRET setting!')

  httpd = oauth2client.tools.ClientRedirectServer(
      ('localhost', 0), oauth2client.tools.ClientRedirectHandler)
  httpd.timeout = 60
  flow = oauth2client.client.OAuth2WebServerFlow(
      client_id=base_settings.OAUTH_CLIENT_ID,
      client_secret=settings.OAUTH_CLIENT_SECRET,
      redirect_uri='http://%s:%s/' % httpd.server_address,
      scope=base_settings.OAUTH_SCOPE,
      )
  authorize_url = flow.step1_get_authorize_url()

  webbrowser.open(authorize_url, new=1, autoraise=True)
  httpd.handle_request()

  if 'error' in httpd.query_params:
    raise AuthenticationError('Authentication request was rejected.')

  try:
    credentials = flow.step2_exchange(
        httpd.query_params,
        http=httplib2.Http(ca_certs=settings.ROOT_CA_CERT_CHAIN_PEM_FILE_PATH))
  except oauth2client.client.FlowExchangeError as e:
    raise AuthenticationError(f'Authentication has failed: {e}')
  else:
    logging.info('Authentication successful!')
    return credentials
