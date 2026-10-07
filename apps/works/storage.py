"""Where Work documents are kept: Cloudinary, as private files that only the CRM can read.

Files are uploaded as raw assets (stored byte for byte; the CRM records each file's type itself) with the
'authenticated' delivery type, so Cloudinary serves them only to signed requests. The CRM fetches a file for someone the
Works API has let in and passes it on: browsers never get a Cloudinary address, the API key or the secret.

Cloudinary's REST API is called through the standard library: the Upload API (signed requests) to store and fetch, the
Admin API (HTTP Basic auth) to delete. The credentials are the CLOUDINARY_* settings, read from the environment.
"""

import base64
import hashlib
import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from django.conf import settings
from django.views.decorators.debug import sensitive_variables

logger = logging.getLogger(__name__)

API_URL = 'https://api.cloudinary.com/v1_1'
RESOURCE_TYPE = 'raw'
DELIVERY_TYPE = 'authenticated'
TIMEOUT = 30  # seconds for each Cloudinary request
DELETE_BATCH = 100  # the Admin API deletes at most 100 files per request
# Seconds a signed download link stays valid. The CRM uses it at once and never shows it; the margin is for a server
# clock that runs a little behind Cloudinary's.
LINK_LIFETIME = 300


class StorageError(Exception):
    """Cloudinary isn't configured, can't be reached, or refused the request. The message is for the server log, and
    never contains the API secret."""


@sensitive_variables('secret')
def _credentials():
    # Without stray spaces (a pasted value), which would break the URL or the signature.
    cloud, key, secret = (
        value.strip()
        for value in (settings.CLOUDINARY_CLOUD_NAME, settings.CLOUDINARY_API_KEY, settings.CLOUDINARY_API_SECRET)
    )
    if not (cloud and key and secret):
        raise StorageError(
            'Cloudinary is not configured: set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET.'
        )
    return cloud, key, secret


def signature(params, secret):
    """Cloudinary's request signature: the params sorted by name as name=value pairs joined with &, then the secret,
    SHA-1 in hex. File, api_key, resource_type and cloud_name are never signed."""
    payload = '&'.join(f'{name}={value}' for name, value in sorted(params.items()))
    return hashlib.sha1((payload + secret).encode()).hexdigest()


@sensitive_variables('secret')
def _signed(params):
    """The cloud name, and `params` with the timestamp, API key and signature an Upload API request needs."""
    cloud, key, secret = _credentials()
    params = {**params, 'timestamp': int(time.time())}
    return cloud, {**params, 'signature': signature(params, secret), 'api_key': key}


def _open(request):
    """Sends the request and returns the open response. Errors become StorageError with Cloudinary's own message; the
    URL, which carries a signature, is left out."""
    try:
        return urllib.request.urlopen(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error)['error']['message']
        except Exception:
            message = error.reason
        raise StorageError(f'Cloudinary answered {error.code}: {message}') from None
    except (urllib.error.URLError, OSError) as error:
        raise StorageError(f"Couldn't reach Cloudinary: {getattr(error, 'reason', error)}") from None
    except (ValueError, http.client.HTTPException) as error:
        # Such as http.client.InvalidURL, whose message would carry the signed URL: only the kind of error is kept.
        raise StorageError(f'The request to Cloudinary failed: {type(error).__name__}') from None


def _call(request):
    """Sends the request and returns Cloudinary's JSON answer. Any failure, reading the answer included (an HTML error
    page from a proxy, a connection dropped half-way), becomes StorageError."""
    with _open(request) as response:
        try:
            answer = json.load(response)
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise StorageError(f"Cloudinary's answer couldn't be read: {error}") from None
    if not isinstance(answer, dict):
        raise StorageError("Cloudinary's answer wasn't understood.")
    return answer


def upload(content, folder, extension):
    """Stores `content` (bytes) as a new private file in `folder`, under an unguessable name ending in `extension` (a
    raw file's public id carries its extension). Returns the public id once Cloudinary has confirmed every byte."""
    public_id = f'{folder}/{uuid.uuid4().hex}{extension}'
    cloud, params = _signed({'public_id': public_id, 'type': DELIVERY_TYPE})
    boundary = uuid.uuid4().hex
    fields = b''.join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in params.items()
    )
    body = b''.join([
        fields,
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="document"\r\n'.encode(),
        b'Content-Type: application/octet-stream\r\n\r\n',
        content,
        f'\r\n--{boundary}--\r\n'.encode(),
    ])
    request = urllib.request.Request(
        f'{API_URL}/{cloud}/{RESOURCE_TYPE}/upload', data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    try:
        stored = _call(request)
    except StorageError:
        # The file may be stored although its answer was lost (a timeout, a dropped connection): nothing will point at
        # it, so it goes. A file that was never stored is simply not found.
        delete([(RESOURCE_TYPE, public_id)])
        raise
    if stored.get('public_id') != public_id or stored.get('bytes') != len(content):
        delete([(RESOURCE_TYPE, stored.get('public_id') or public_id)])
        raise StorageError(
            f'Cloudinary stored {stored.get("bytes")} of {len(content)} bytes, as {stored.get("public_id")}.'
        )
    return public_id


def fetch(public_id, resource_type=RESOURCE_TYPE):
    """The stored file, as an open HTTP response to read and close: fetched through a download link signed for a few
    minutes, used at once by the server and never shown to anyone."""
    expires_at = int(time.time()) + LINK_LIFETIME
    cloud, params = _signed({'public_id': public_id, 'type': DELIVERY_TYPE, 'expires_at': expires_at})
    return _open(urllib.request.Request(f'{API_URL}/{cloud}/{resource_type}/download?{urllib.parse.urlencode(params)}'))


@sensitive_variables('secret', 'authorization')
def delete(files):
    """Deletes stored files, given as (resource type, public id) pairs, up to 100 per request. Never raises: it runs
    once the records are already gone, so a failure is logged with the ids, for removing them by hand."""
    by_type = {}
    for resource_type, public_id in files:
        by_type.setdefault(resource_type, []).append(public_id)
    for resource_type, public_ids in by_type.items():
        for start in range(0, len(public_ids), DELETE_BATCH):
            batch = public_ids[start:start + DELETE_BATCH]
            try:
                cloud, key, secret = _credentials()
                authorization = 'Basic ' + base64.b64encode(f'{key}:{secret}'.encode()).decode()
                request = urllib.request.Request(
                    f'{API_URL}/{cloud}/resources/{resource_type}/{DELIVERY_TYPE}',
                    data=json.dumps({'public_ids': batch}).encode(),
                    headers={'Authorization': authorization, 'Content-Type': 'application/json'}, method='DELETE',
                )
                deleted = _call(request).get('deleted', {})
                # A file that is already gone counts as deleted.
                left = [public_id for public_id in batch if deleted.get(public_id) not in ('deleted', 'not_found')]
            except Exception as error:
                logger.error('Cloudinary files not deleted (%s): %s', ', '.join(batch), error)
                continue
            if left:
                logger.error('Cloudinary files not deleted (%s): Cloudinary kept them.', ', '.join(left))
