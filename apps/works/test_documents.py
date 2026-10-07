"""Work documents: which documents a Work needs (documents.py), the Cloudinary client (storage.py) and the Works API's
documents endpoints. Nothing reaches Cloudinary: the API tests replace storage.py's functions, while the client tests
and SecretHygieneTests replace urlopen, so the real client code runs against a stand-in."""

import base64
import email.policy
import io
import json
import time
import urllib.error
import urllib.parse
import uuid
from email.parser import BytesParser
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.activities.models import Activity
from apps.leads.models import SolarPlan
from apps.maintenance.reset import crm_record_counts, reset_crm_data

from . import documents, storage
from .models import Work, WorkDocument, WorkStage
from .tests import PASSWORD, convert_lead

# How each accepted type's contents start (documents.FILE_TYPES), with bytes a careless parser would mangle.
PDF = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\r\n1 0 obj << >> endobj\n%%EOF\n'
PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + bytes(16)
JPEG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + bytes(16)
WEBP = b'RIFF\x24\x00\x00\x00WEBPVP8 ' + bytes(16)

LOAN_KEYS = [
    'bank_passbook', 'aadhar_card', 'pan_card', 'electricity_bill', 'email_id', 'phone_number', 'site_photo',
    'land_tax_paper', 'name_signature',
]
SUBSIDY_KEYS = ['dcr', 'gps_photo', 'inverter_serial_number']
FILE_KEYS = [key for key in LOAN_KEYS + SUBSIDY_KEYS if key not in ('email_id', 'phone_number')]
FILE_NAMES = [
    'Bank passbook', 'Aadhaar card', 'PAN card', 'Electricity bill', 'Site photo', 'Land tax paper',
    'Name & signature (white paper)', 'DCR', 'GPS photo', 'Inverter serial number',
]
ACCEPTED = ['.pdf', '.jpg', '.jpeg', '.png', '.webp']
ENDPOINTS = ['checklist', 'upload', 'file', 'delete']

SECRET = 'test-secret-value'
CLOUDINARY = {'CLOUDINARY_CLOUD_NAME': 'demo-cloud', 'CLOUDINARY_API_KEY': '1234', 'CLOUDINARY_API_SECRET': SECRET}
URLOPEN = 'apps.works.storage.urllib.request.urlopen'
DELETE_URL = 'https://api.cloudinary.com/v1_1/demo-cloud/resources/raw/authenticated'


def basic_auth(secret=SECRET):
    return 'Basic ' + base64.b64encode(f'1234:{secret}'.encode()).decode()


def answer(data):
    """Cloudinary's JSON answer, as urlopen returns it: a file to read in a with block."""
    return io.BytesIO(json.dumps(data).encode())


def refusal(url, code, message):
    """Cloudinary refusing a request, as urlopen raises it."""
    body = io.BytesIO(json.dumps({'error': {'message': message}}).encode())
    return urllib.error.HTTPError(url, code, 'Refused', {}, body)


class LostConnection(io.BytesIO):
    """An answer whose connection drops while it is read."""

    def read(self, *args):
        raise ConnectionResetError('Connection reset by peer')


def unreadable_answers():
    """Answers that aren't Cloudinary's JSON: from something else on the way (a proxy's error page), or cut off."""
    return [('not JSON', io.BytesIO(b'<html>502 Bad Gateway</html>')), ('cut off', LostConnection())]


def upload_fields(request):
    """An Upload API request's multipart fields by name: text, and the file as bytes."""
    head = f'Content-Type: {request.get_header("Content-type")}\r\n\r\n'.encode()
    fields = {}
    for part in BytesParser(policy=email.policy.HTTP).parsebytes(head + request.data).iter_parts():
        value = part.get_payload(decode=True)
        fields[part.get_param('name', header='content-disposition')] = value if part.get_filename() else value.decode()
    return fields


def download_params(request):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query))


class FakeCloudinary:
    """Cloudinary's API as storage.py uses it, in memory and in place of urlopen, so the real client code runs. Like
    Cloudinary, it refuses requests not signed (or authorised) with its secret. Keeps the stored files, and records
    every request and every signature it is sent."""

    def __init__(self, secret=SECRET):
        self.secret, self.files, self.requests, self.signatures = secret, {}, [], []

    def __call__(self, request, *, timeout):  # every request must have a timeout
        self.requests.append(request)
        if request.get_method() == 'DELETE':
            if request.get_header('Authorization') != basic_auth(self.secret):
                raise refusal(request.full_url, 401, 'Invalid credentials')
            deleted = {
                public_id: 'deleted' if self.files.pop(public_id, None) is not None else 'not_found'
                for public_id in json.loads(request.data)['public_ids']
            }
            return answer({'deleted': deleted})
        uploading = request.full_url.endswith('/upload')
        params = upload_fields(request) if uploading else download_params(request)
        self.signatures.append(params['signature'])
        signed = {name: value for name, value in params.items() if name not in ('file', 'api_key', 'signature')}
        if params['api_key'] != '1234' or params['signature'] != storage.signature(signed, self.secret):
            raise refusal(request.full_url, 401, 'Invalid Signature')
        if uploading:
            self.files[params['public_id']] = params['file']
            return answer({'public_id': params['public_id'], 'bytes': len(params['file'])})
        return io.BytesIO(self.files[params['public_id']])


def add_document(work, key, user, **fields):
    """A document as an upload records it, straight into the database."""
    return WorkDocument.objects.create(**{
        'work': work, 'document_key': key, 'uploaded_by': user, 'cloudinary_resource_type': 'raw',
        'cloudinary_public_id': f'ragno/works/{work.pk}/{key}/{uuid.uuid4().hex}.pdf',
        'original_filename': f'{key}.pdf', 'content_type': 'application/pdf', 'file_size': len(PDF), **fields,
    })


def checklist_item(checklist, key):
    """The Documents page's entry for `key`."""
    return next(item for group in checklist['groups'] for item in group['items'] if item['key'] == key)


class RequirementTests(SimpleTestCase):
    def test_there_are_twelve_required_documents_in_two_groups_in_the_documents_page_order(self):
        keys = [requirement.key for requirement in documents.REQUIREMENTS]
        groups = {
            group: [requirement.key for requirement in documents.REQUIREMENTS if requirement.group == group]
            for group, _ in documents.GROUPS
        }

        self.assertEqual((len(keys), len(set(keys))), (12, 12))
        self.assertEqual(groups, {'LOAN': LOAN_KEYS, 'SUBSIDY': SUBSIDY_KEYS})
        self.assertTrue(all(requirement.required for requirement in documents.REQUIREMENTS))

    def test_email_id_and_phone_number_are_the_works_own_fields_and_the_other_ten_are_files(self):
        fields = {requirement.key: requirement.field for requirement in documents.REQUIREMENTS if requirement.field}

        self.assertEqual(fields, {'email_id': 'email', 'phone_number': 'phone'})
        self.assertEqual(documents.FILE_DOCUMENTS, list(zip(FILE_KEYS, FILE_NAMES)))
        self.assertEqual(WorkDocument._meta.get_field('document_key').choices, documents.FILE_DOCUMENTS)

    def test_keys_are_url_safe_identifiers_and_never_the_display_names(self):
        for requirement in documents.REQUIREMENTS:
            with self.subTest(requirement.key):
                # Every key routes (the URL's key pattern) and fits the database column.
                reverse('work-upload-document', args=[1, requirement.key])
                self.assertLessEqual(len(requirement.key), WorkDocument._meta.get_field('document_key').max_length)
        names = {requirement.name for requirement in documents.REQUIREMENTS}
        self.assertFalse(names & {requirement.key for requirement in documents.REQUIREMENTS})


class SignatureTests(SimpleTestCase):
    def test_the_signature_reproduces_cloudinarys_documented_examples(self):
        self.assertEqual(
            storage.signature({'timestamp': 1315060510}, 'abcd'), 'a21ad0f63beb4de2e5575204b79ab90bffb02c10',
        )
        self.assertEqual(
            storage.signature(
                {'timestamp': 1315060510, 'public_id': 'sample_image', 'eager': 'w_400,h_300,c_pad|w_260,h_200,c_crop'},
                'abcd',
            ),
            'bfd09f95f331f558cbd1320e67aa8d488770583e',
        )


@override_settings(**CLOUDINARY)
class StorageClientTests(SimpleTestCase):
    """storage.py's requests as Cloudinary receives them, from the real code: only urlopen is replaced."""

    def assert_secret_not_sent(self, request):
        self.assertNotIn(SECRET, request.full_url)
        self.assertNotIn(SECRET, str(request.header_items()))
        self.assertNotIn(SECRET.encode(), request.data or b'')

    def test_upload_sends_a_signed_multipart_request_and_returns_the_public_id(self):
        content = PDF + b'\x00\xff\r\n--not-the-boundary\r\ntail'
        cloudinary = FakeCloudinary()
        started = int(time.time())

        with mock.patch(URLOPEN, cloudinary):
            public_id = storage.upload(content, 'ragno/works/7/pan_card', '.pdf')

        [request] = cloudinary.requests
        self.assertEqual(
            (request.get_method(), request.full_url), ('POST', 'https://api.cloudinary.com/v1_1/demo-cloud/raw/upload'),
        )
        fields = upload_fields(request)
        self.assertRegex(fields['public_id'], r'^ragno/works/7/pan_card/[0-9a-f]{32}\.pdf$')
        self.assertEqual(public_id, fields['public_id'])
        self.assertEqual((fields['type'], fields['api_key'], fields['file']), ('authenticated', '1234', content))
        self.assertTrue(started <= int(fields['timestamp']) <= time.time())
        signed = {name: value for name, value in fields.items() if name not in ('file', 'api_key', 'signature')}
        self.assertEqual(set(signed), {'public_id', 'type', 'timestamp'})
        self.assertEqual(fields['signature'], storage.signature(signed, SECRET))
        self.assert_secret_not_sent(request)

    def test_upload_fails_and_deletes_the_stray_file_when_cloudinary_did_not_store_every_byte(self):
        def short_by_one(request, timeout):
            if request.get_method() == 'DELETE':
                return answer({'deleted': dict.fromkeys(json.loads(request.data)['public_ids'], 'deleted')})
            fields = upload_fields(request)
            return answer({'public_id': fields['public_id'], 'bytes': len(fields['file']) - 1})

        with mock.patch(URLOPEN, side_effect=short_by_one) as urlopen, self.assertRaises(storage.StorageError):
            storage.upload(PDF, 'ragno/works/7/pan_card', '.pdf')

        upload, delete = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual((delete.get_method(), delete.full_url), ('DELETE', DELETE_URL))
        self.assertEqual(json.loads(delete.data), {'public_ids': [upload_fields(upload)['public_id']]})

    def test_fetch_downloads_through_a_link_signed_for_a_few_minutes(self):
        cloudinary = FakeCloudinary()
        cloudinary.files['ragno/works/7/pan_card/abc.pdf'] = PDF

        with mock.patch(URLOPEN, cloudinary), storage.fetch('ragno/works/7/pan_card/abc.pdf', 'raw') as stored:
            self.assertEqual(stored.read(), PDF)

        [request] = cloudinary.requests
        url, params = urllib.parse.urlsplit(request.full_url), download_params(request)
        self.assertEqual(
            (request.get_method(), url.scheme, url.netloc, url.path),
            ('GET', 'https', 'api.cloudinary.com', '/v1_1/demo-cloud/raw/download'),
        )
        self.assertEqual(set(params), {'public_id', 'type', 'timestamp', 'expires_at', 'api_key', 'signature'})
        self.assertEqual(
            (params['public_id'], params['type'], params['api_key']),
            ('ragno/works/7/pan_card/abc.pdf', 'authenticated', '1234'),
        )
        self.assertAlmostEqual(int(params['expires_at']), time.time() + storage.LINK_LIFETIME, delta=5)
        signed = {name: value for name, value in params.items() if name not in ('api_key', 'signature')}
        self.assertEqual(params['signature'], storage.signature(signed, SECRET))
        self.assert_secret_not_sent(request)

    def test_delete_sends_at_most_100_public_ids_per_request_with_basic_auth(self):
        public_ids = [f'ragno/works/7/dcr/{n:032x}.pdf' for n in range(250)]
        cloudinary = FakeCloudinary()
        # Half are already gone: Cloudinary answers 'not_found' for those, which counts as deleted.
        cloudinary.files = dict.fromkeys(public_ids[::2], PDF)

        with mock.patch(URLOPEN, cloudinary), self.assertNoLogs('apps.works.storage'):
            storage.delete([('raw', public_id) for public_id in public_ids])

        batches = []
        for request in cloudinary.requests:
            self.assertEqual((request.get_method(), request.full_url), ('DELETE', DELETE_URL))
            self.assertEqual(request.get_header('Content-type'), 'application/json')
            self.assertEqual(request.get_header('Authorization'), basic_auth())
            self.assertNotIn(SECRET, request.full_url)
            self.assertNotIn(SECRET.encode(), request.data)
            batches.append(json.loads(request.data)['public_ids'])
        self.assertEqual([len(batch) for batch in batches], [100, 100, 50])
        self.assertEqual(sum(batches, []), public_ids)
        self.assertEqual(cloudinary.files, {})

    def test_delete_never_raises_and_logs_the_public_ids_it_could_not_delete(self):
        public_ids = ['ragno/works/7/dcr/a.pdf', 'ragno/works/7/dcr/b.pdf']
        failures = [
            ('refused', {'side_effect': refusal(DELETE_URL, 401, 'Invalid credentials')}, public_ids),
            ('unreachable', {'side_effect': urllib.error.URLError('timed out')}, public_ids),
            ('partly deleted', {'return_value': answer({'deleted': {public_ids[0]: 'deleted'}})}, public_ids[1:]),
        ]
        for failure, behaviour, left in failures:
            with self.subTest(failure):
                with mock.patch(URLOPEN, **behaviour), self.assertLogs('apps.works.storage', 'ERROR') as logs:
                    storage.delete([('raw', public_id) for public_id in public_ids])

                [line] = logs.output
                for public_id in left:
                    self.assertIn(public_id, line)
                self.assertNotIn(SECRET, line)
                self.assertNotIn(basic_auth(), line)

    def test_a_failed_batch_does_not_stop_the_next_ones(self):
        public_ids = [f'ragno/works/7/dcr/{n:032x}.pdf' for n in range(150)]
        answers = [refusal(DELETE_URL, 500, 'General error'), answer({'deleted': dict.fromkeys(public_ids, 'deleted')})]

        with (
            mock.patch(URLOPEN, side_effect=answers) as urlopen,
            self.assertLogs('apps.works.storage', 'ERROR') as logs,
        ):
            storage.delete([('raw', public_id) for public_id in public_ids])

        self.assertEqual(urlopen.call_count, 2)
        [line] = logs.output
        self.assertIn(public_ids[0], line)
        self.assertNotIn(public_ids[100], line)

    def test_delete_never_raises_even_when_cloudinarys_answer_cannot_be_read(self):
        # delete() runs in on_commit callbacks, once the records are gone: raising there would turn a request that has
        # already committed into a 500, and skip the callbacks after it.
        for failure, unreadable in unreadable_answers():
            with self.subTest(failure):
                with (
                    mock.patch(URLOPEN, return_value=unreadable),
                    self.assertLogs('apps.works.storage', 'ERROR') as logs,
                ):
                    storage.delete([('raw', 'ragno/works/7/dcr/a.pdf')])

                self.assertIn('ragno/works/7/dcr/a.pdf', logs.output[0])

    def test_an_unreadable_answer_to_an_upload_is_a_storage_error_and_the_file_is_deleted_in_case_it_was_stored(self):
        # So the view answers its 502 with a message for the screen, rather than a 500.
        for failure, unreadable in unreadable_answers():
            cloudinary = FakeCloudinary()
            answers = [unreadable]

            def urlopen(request, *, timeout):
                # The upload's answer is lost on the way; the deletion that follows reaches Cloudinary.
                return answers.pop() if answers else cloudinary(request, timeout=timeout)

            with (
                self.subTest(failure),
                mock.patch(URLOPEN, side_effect=urlopen) as patched,
                self.assertRaises(storage.StorageError),
                self.assertNoLogs('apps.works.storage'),
            ):
                storage.upload(PDF, 'ragno/works/7/pan_card', '.pdf')

            upload, delete = [call.args[0] for call in patched.call_args_list]
            # The file may be stored although its answer was lost: nothing will point at it, so it goes.
            self.assertEqual(json.loads(delete.data), {'public_ids': [upload_fields(upload)['public_id']]})

    def test_without_all_three_settings_nothing_is_sent_and_the_error_names_the_settings_not_their_values(self):
        unset = {'CLOUDINARY_CLOUD_NAME': '', 'CLOUDINARY_API_KEY': '', 'CLOUDINARY_API_SECRET': ''}
        for missing in (unset, {'CLOUDINARY_API_SECRET': ''}):
            with self.subTest(missing=list(missing)), self.settings(**missing), mock.patch(URLOPEN) as urlopen:
                with self.assertRaises(storage.StorageError) as uploading:
                    storage.upload(PDF, 'ragno/works/7/dcr', '.pdf')
                with self.assertRaises(storage.StorageError) as fetching:
                    storage.fetch('ragno/works/7/dcr/a.pdf')
                with self.assertLogs('apps.works.storage', 'ERROR') as logs:
                    storage.delete([('raw', 'ragno/works/7/dcr/a.pdf')])  # logged, never raised

                urlopen.assert_not_called()
                self.assertIn('ragno/works/7/dcr/a.pdf', logs.output[0])
                for message in (str(uploading.exception), str(fetching.exception), logs.output[0]):
                    for setting in ('CLOUDINARY_CLOUD_NAME', 'CLOUDINARY_API_KEY', 'CLOUDINARY_API_SECRET'):
                        self.assertIn(setting, message)
                    self.assertNotIn('demo-cloud', message)
                    self.assertNotIn('1234', message)

    def test_a_failed_request_reports_cloudinarys_message_but_never_the_signed_url(self):
        signed_url = (
            'https://api.cloudinary.com/v1_1/demo-cloud/raw/download?public_id=x&signature=0f1e2d3c&api_key=1234'
        )
        failures = [
            (refusal(signed_url, 401, 'Invalid Signature'), 'Cloudinary answered 401: Invalid Signature'),
            (
                urllib.error.HTTPError(signed_url, 502, 'Bad Gateway', {}, io.BytesIO(b'<html>Bad Gateway</html>')),
                'Cloudinary answered 502: Bad Gateway',
            ),
            (urllib.error.URLError('timed out'), "Couldn't reach Cloudinary: timed out"),
        ]
        for error, expected in failures:
            with self.subTest(expected):
                with mock.patch(URLOPEN, side_effect=error), self.assertRaises(storage.StorageError) as raised:
                    storage.fetch('ragno/works/7/dcr/a.pdf')

                message = str(raised.exception)
                self.assertEqual(message, expected)
                for leak in (signed_url, '0f1e2d3c', 'api_key', SECRET):
                    self.assertNotIn(leak, message)


@override_settings(**CLOUDINARY)
class StorageClientEdgeTests(SimpleTestCase):
    def test_a_request_python_refuses_to_send_is_a_storage_error_without_its_url(self):
        # http.client.InvalidURL (a ValueError) quotes the whole signed URL in its message.
        refused = ValueError("URL can't contain control characters. '/v1_1/demo-cloud/raw/download?signature=0f1e2d3c'")

        with mock.patch(URLOPEN, side_effect=refused), self.assertRaises(storage.StorageError) as raised:
            storage.fetch('ragno/works/7/pan_card/abc.pdf')

        self.assertEqual(str(raised.exception), 'The request to Cloudinary failed: ValueError')

    def test_stray_spaces_around_the_settings_are_ignored(self):
        cloudinary = FakeCloudinary()
        spaced = {name: f' {value} ' for name, value in CLOUDINARY.items()}

        with self.settings(**spaced), mock.patch(URLOPEN, cloudinary):
            public_id = storage.upload(PDF, 'ragno/works/7/pan_card', '.pdf')

        self.assertEqual(cloudinary.files[public_id], PDF)
        self.assertTrue(cloudinary.requests[0].full_url.startswith('https://api.cloudinary.com/v1_1/demo-cloud/'))


class FileCheckTests(SimpleTestCase):
    def test_check_file_refuses_a_file_over_10_mb_by_its_size(self):
        def upload(name, size):
            return SimpleNamespace(name=name, size=size, read=lambda count=-1: PDF[:count], seek=lambda offset: offset)

        with self.assertRaisesMessage(ValidationError, 'The file is larger than 10 MB.'):
            documents.check_file(upload('big.pdf', documents.MAX_FILE_SIZE + 1))
        self.assertEqual(documents.check_file(upload('max.PDF', documents.MAX_FILE_SIZE)), ('application/pdf', '.pdf'))


class DocumentTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff = User.objects.create_user(email='staff@example.com', password=PASSWORD, name='Priya Nair')
        cls.plan = SolarPlan.objects.get(capacity=5)
        cls.work = convert_lead(cls.plan, cls.admin, email='asha@example.com')

    def setUp(self):
        # Cloudinary is never called: storage.py's functions are replaced, keeping their signatures. An upload gets a
        # new public id, as Cloudinary's would, and every file fetched is a PDF.
        self.storage_upload = self.replace(
            'upload', side_effect=lambda content, folder, extension: f'{folder}/{uuid.uuid4().hex}{extension}',
        )
        self.storage_fetch = self.replace('fetch', side_effect=lambda public_id, resource_type: io.BytesIO(PDF))
        self.storage_delete = self.replace('delete')

    def replace(self, name, **kwargs):
        patcher = mock.patch.object(storage, name, autospec=True, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def staff_with_work_module(self):
        Role.objects.get(pk=Role.STAFF).permissions.add(Permission.objects.get(codename='access_work'))
        # Read again: Role.permission_names is cached on the role the old copy holds.
        return User.objects.get(pk=self.staff.pk)

    def documents_url(self, work=None):
        return reverse('work-documents', args=[(work or self.work).pk])

    def document_url(self, key, work=None):
        return reverse('work-upload-document', args=[(work or self.work).pk, key])

    def file_url(self, key, work=None):
        return reverse('work-document-file', args=[(work or self.work).pk, key])

    def upload(self, key, name='scan.pdf', content=PDF, content_type='application/pdf', work=None):
        file = SimpleUploadedFile(name, content, content_type=content_type)
        return self.client.post(self.document_url(key, work), {'file': file}, format='multipart')


class DocumentChecklistTests(DocumentTestCase):
    def test_a_new_work_lists_every_required_document_in_its_group_with_how_complete_they_are(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.documents_url())

        self.assertEqual(response.status_code, 200, response.data)
        groups = response.data['groups']
        self.assertEqual(
            [(group['key'], group['label'], [item['key'] for item in group['items']]) for group in groups],
            [('LOAN', 'Customer / loan documents', LOAN_KEYS), ('SUBSIDY', 'Subsidy documents', SUBSIDY_KEYS)],
        )
        items = {item['key']: item for group in groups for item in group['items']}
        for key, name, field in [('email_id', 'Email ID', 'email'), ('phone_number', 'Phone number', 'phone')]:
            self.assertEqual(items[key], {
                'key': key, 'name': name, 'required': True, 'kind': 'field', 'field': field, 'provided': True,
                'accept': [], 'document': None,
            })
        for key, name in zip(FILE_KEYS, FILE_NAMES):
            self.assertEqual(items[key], {
                'key': key, 'name': name, 'required': True, 'kind': 'file', 'field': None, 'provided': False,
                'accept': ACCEPTED, 'document': None,
            })
        self.assertEqual(response.data['max_file_size'], 10485760)
        self.assertEqual(response.data['work']['id'], self.work.pk)
        self.assertEqual(response.data['work']['document_summary'], {
            'required_count': 12, 'completed_count': 2, 'missing_count': 10, 'is_complete': False,
            'missing_documents': FILE_NAMES,
        })
        self.assertIs(response.data['can_delete'], True)

    def test_only_admins_are_offered_deleting(self):
        self.client.force_authenticate(self.staff_with_work_module())

        self.assertIs(self.client.get(self.documents_url()).data['can_delete'], False)


class DocumentUploadTests(DocumentTestCase):
    def test_admins_and_staff_with_the_work_module_upload_pdf_jpeg_png_and_webp_files(self):
        staff = self.staff_with_work_module()
        cases = [
            (self.admin, 'bank_passbook', 'Passbook.PDF', PDF, 'application/pdf', '.pdf'),
            (staff, 'aadhar_card', 'aadhaar.png', PNG, 'image/png', '.png'),
            (self.admin, 'pan_card', 'pan card.jpeg', JPEG, 'image/jpeg', '.jpeg'),
            (staff, 'site_photo', 'roof.jpg', JPEG, 'image/jpeg', '.jpg'),
            (staff, 'gps_photo', 'gps.webp', WEBP, 'image/webp', '.webp'),
        ]
        for uploaded, (user, key, name, content, content_type, extension) in enumerate(cases, start=1):
            with self.subTest(key=key):
                self.client.force_authenticate(user)
                self.storage_upload.reset_mock()

                # The type the browser claims is ignored: the file's contents decide it.
                response = self.upload(key, name, content, content_type='text/html')

                self.assertEqual(response.status_code, 200, response.data)
                self.storage_upload.assert_called_once_with(content, f'ragno/works/{self.work.pk}/{key}', extension)
                document = WorkDocument.objects.get(work=self.work, document_key=key)
                self.assertEqual(
                    (document.content_type, document.original_filename, document.file_size, document.uploaded_by,
                     document.cloudinary_resource_type),
                    (content_type, name, len(content), user, 'raw'),
                )
                item = checklist_item(response.data, key)
                self.assertTrue(item['provided'])
                self.assertEqual({**item['document'], 'uploaded_at': None}, {
                    'original_filename': name, 'content_type': content_type, 'file_size': len(content),
                    'uploaded_at': None, 'uploaded_by_name': user.name,
                })
                self.assertTrue(item['document']['uploaded_at'])
                self.assertEqual(response.data['work']['document_summary']['missing_count'], 10 - uploaded)
                # Where the file is kept never reaches the browser.
                body = response.content.decode()
                self.assertNotIn('cloudinary', body.lower())
                self.assertNotIn(document.cloudinary_public_id, body)

    def test_the_stored_name_is_the_uploaded_one_without_its_path_or_invisible_characters(self):
        self.client.force_authenticate(self.admin)

        # A right-to-left override would show this name as "invoicefdp.jpg".
        response = self.upload('pan_card', 'C:\\scans\\invoice\N{RIGHT-TO-LEFT OVERRIDE}gpj.pdf')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(WorkDocument.objects.get().original_filename, 'invoicegpj.pdf')

    def test_uploading_again_replaces_the_document_and_deletes_the_old_file_once_that_commits(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.upload('pan_card', 'old.pdf').status_code, 200)
        old = WorkDocument.objects.get()
        self.client.force_authenticate(self.staff_with_work_module())

        with (
            self.assertLogs('apps.works.views', 'WARNING') as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.upload('pan_card', 'new.png', PNG, 'image/png')
            # Until the replacement commits, the old file is the one on record.
            self.storage_delete.assert_not_called()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(logs.output, [
            f'WARNING:apps.works.views:Work {self.work.pk}: pan_card document replaced by user {self.staff.pk} '
            f'(old file {old.cloudinary_public_id})',
        ])
        # Only the old file goes: never the new one.
        self.storage_delete.assert_called_once_with([('raw', old.cloudinary_public_id)])
        new = WorkDocument.objects.get()
        self.assertEqual(new.pk, old.pk)
        self.assertNotEqual(new.cloudinary_public_id, old.cloudinary_public_id)
        self.assertEqual(
            (new.original_filename, new.content_type, new.uploaded_by), ('new.png', 'image/png', self.staff),
        )
        self.assertGreaterEqual(new.uploaded_at, old.uploaded_at)
        self.assertEqual(new.created_at, old.created_at)
        self.assertEqual(checklist_item(response.data, 'pan_card')['document']['uploaded_by_name'], 'Priya Nair')


class DocumentUploadFailureTests(DocumentTestCase):
    NEW = 'ragno/works/new/pan_card/new.pdf'

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def test_a_cloudinary_failure_answers_502_with_a_message_for_the_screen_and_records_nothing(self):
        self.storage_upload.side_effect = storage.StorageError('Cloudinary answered 401: Invalid Signature')

        with self.assertLogs('apps.works.views', 'ERROR') as logs, self.assertLogs('django.request', 'ERROR'):
            response = self.upload('pan_card')

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data, {'detail': 'Document upload failed. Please try again.'})
        self.assertNotIn('Invalid Signature', response.content.decode())
        self.assertFalse(WorkDocument.objects.exists())
        self.assertIn('Invalid Signature', logs.output[0])  # the reason stays in the server log

    def test_a_database_failure_after_the_upload_deletes_the_new_file_at_once(self):
        self.storage_upload.side_effect = [self.NEW]

        with (
            mock.patch.object(WorkDocument, 'save_upload', side_effect=IntegrityError('could not save')),
            self.captureOnCommitCallbacks(execute=True) as callbacks,
            self.assertLogs('django.request', 'ERROR'),
            self.assertRaises(IntegrityError),
        ):
            self.upload('pan_card')

        # Nothing records the new file, so it goes straight away, not once a commit that never comes.
        self.storage_delete.assert_called_once_with([('raw', self.NEW)])
        self.assertEqual(callbacks, [])
        self.assertFalse(WorkDocument.objects.exists())

    def test_a_failed_replacement_keeps_the_old_document_and_its_file(self):
        self.upload('pan_card', 'old.pdf')
        old = WorkDocument.objects.get()
        self.storage_upload.side_effect = [self.NEW]

        with (
            mock.patch.object(WorkDocument, 'save_upload', side_effect=IntegrityError('could not save')),
            self.captureOnCommitCallbacks(execute=True) as callbacks,
            self.assertLogs('django.request', 'ERROR'),
            self.assertRaises(IntegrityError),
        ):
            self.upload('pan_card', 'new.pdf')

        self.storage_delete.assert_called_once_with([('raw', self.NEW)])
        self.assertEqual(callbacks, [])
        kept = WorkDocument.objects.get()
        self.assertEqual((kept.cloudinary_public_id, kept.original_filename), (old.cloudinary_public_id, 'old.pdf'))

    def test_a_file_cloudinary_cannot_send_answers_502(self):
        add_document(self.work, 'pan_card', self.admin)
        self.storage_fetch.side_effect = storage.StorageError("Couldn't reach Cloudinary: timed out")

        with self.assertLogs('apps.works.views', 'ERROR') as logs, self.assertLogs('django.request', 'ERROR'):
            response = self.client.get(self.file_url('pan_card'))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data, {'detail': "The document couldn't be loaded. Please try again."})
        self.assertIn('timed out', logs.output[0])


class DocumentValidationTests(DocumentTestCase):
    """A refused upload never reaches Cloudinary and records nothing."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def assert_refused(self, response, message):
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(message, str(response.data['file']))
        self.storage_upload.assert_not_called()
        self.assertFalse(WorkDocument.objects.exists())

    def test_a_request_without_a_file_is_refused(self):
        response = self.client.post(self.document_url('pan_card'), {}, format='multipart')

        self.assert_refused(response, 'Choose a file to upload.')

    def test_an_empty_file_is_refused(self):
        self.assert_refused(self.upload('pan_card', 'empty.pdf', b''), 'The file is empty.')

    def test_a_file_that_is_not_a_pdf_or_an_image_is_refused_whatever_its_name(self):
        for name, content in [
            ('notes.pdf', b'Account number 000123'),
            ('page.png', b'<html><script>alert(1)</script></html>'),
            ('logo.svg', b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
            ('photo.gif', b'GIF89a' + bytes(16)),
        ]:
            with self.subTest(name=name):
                self.assert_refused(self.upload('pan_card', name, content), 'Upload a PDF, JPG, PNG or WebP file.')

    def test_a_file_whose_name_does_not_match_its_contents_is_refused(self):
        mismatched = [('photo.pdf', PNG), ('scan.png', PDF), ('scan', PDF), ('scan.pdf.exe', PDF), ('scan.html', PDF)]
        for name, content in mismatched:
            with self.subTest(name=name):
                response = self.upload('pan_card', name, content)
                self.assert_refused(response, "The file's name doesn't match its contents.")

    def test_a_file_over_10_mb_is_refused_before_the_request_body_is_read(self):
        with mock.patch.object(MultiPartParser, 'parse') as parse:
            response = self.upload('pan_card', 'big.pdf', PDF + bytes(11 * 1024 * 1024))

        self.assert_refused(response, 'The file is larger than 10 MB.')
        parse.assert_not_called()

    def test_a_file_of_exactly_10_mb_is_accepted(self):
        response = self.upload('pan_card', 'max.pdf', PDF.ljust(documents.MAX_FILE_SIZE, b'\0'))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(WorkDocument.objects.get().file_size, documents.MAX_FILE_SIZE)

    def test_only_the_ten_file_documents_can_be_uploaded(self):
        # Email ID and Phone number are the Work's fields, and a key is never a display name's spelling.
        for key in ('email_id', 'phone_number', 'passport', 'aadhaar_card'):
            with self.subTest(key=key):
                self.assertEqual(self.upload(key).status_code, 404)
        self.storage_upload.assert_not_called()

    def test_the_file_must_be_sent_as_multipart_form_data(self):
        response = self.client.post(self.document_url('pan_card'), {'file': 'JVBERi0xLjQK'}, format='json')

        self.assertEqual(response.status_code, 415, response.data)
        self.storage_upload.assert_not_called()


class DocumentFileTests(DocumentTestCase):
    def test_the_file_is_passed_on_inline_with_its_type_and_name_and_never_cached(self):
        document = add_document(
            self.work, 'aadhar_card', self.admin, original_filename='Aadhaar Asha.png', content_type='image/png',
            file_size=len(PNG),
        )
        self.storage_fetch.side_effect = lambda public_id, resource_type: io.BytesIO(PNG)
        self.client.force_authenticate(self.staff_with_work_module())

        response = self.client.get(self.file_url('aadhar_card'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), PNG)
        self.storage_fetch.assert_called_once_with(document.cloudinary_public_id, 'raw')
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['Content-Disposition'], 'inline; filename="Aadhaar Asha.png"')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    def test_a_name_in_any_script_is_sent_encoded(self):
        add_document(self.work, 'pan_card', self.admin, original_filename='ആധാർ കാർഡ്.pdf')
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.file_url('pan_card'))

        self.assertEqual(
            response['Content-Disposition'], "inline; filename*=utf-8''" + urllib.parse.quote('ആധാർ കാർഡ്.pdf'),
        )


class DocumentAccessTests(DocumentTestCase):
    """A document is reached only through its Work, after the Work's own access check: the Work module, and an admin to
    delete one. Works are shared by everyone with the module in this CRM (no per-assignee restriction), and so are their
    documents."""

    def statuses(self, work=None):
        """Each documents endpoint's answer for `work`'s PAN card, with any file deletion it scheduled run."""
        with self.captureOnCommitCallbacks(execute=True):
            return {
                'checklist': self.client.get(self.documents_url(work)).status_code,
                'upload': self.upload('pan_card', work=work).status_code,
                'file': self.client.get(self.file_url('pan_card', work)).status_code,
                'delete': self.client.delete(self.document_url('pan_card', work)).status_code,
            }

    def assert_storage_untouched(self):
        self.storage_upload.assert_not_called()
        self.storage_fetch.assert_not_called()
        self.storage_delete.assert_not_called()

    def test_signed_out_requests_are_refused(self):
        add_document(self.work, 'pan_card', self.admin)

        self.assertEqual(self.statuses(), dict.fromkeys(ENDPOINTS, 401))
        self.assert_storage_untouched()
        self.assertEqual(WorkDocument.objects.count(), 1)

    def test_staff_without_the_work_module_are_refused(self):
        add_document(self.work, 'pan_card', self.admin)
        self.client.force_authenticate(self.staff)

        self.assertEqual(self.statuses(), dict.fromkeys(ENDPOINTS, 403))
        self.assert_storage_untouched()
        self.assertEqual(WorkDocument.objects.count(), 1)

    def test_staff_with_the_work_module_list_upload_and_open_documents_but_cannot_delete_them(self):
        self.client.force_authenticate(self.staff_with_work_module())

        self.assertEqual(self.statuses(), {'checklist': 200, 'upload': 200, 'file': 200, 'delete': 403})
        self.assertTrue(WorkDocument.objects.filter(work=self.work, document_key='pan_card').exists())
        self.storage_delete.assert_not_called()

    def test_an_admin_deletes_a_document_and_then_its_file(self):
        document = add_document(self.work, 'pan_card', self.staff)
        self.client.force_authenticate(self.admin)

        with self.assertLogs('apps.works.views', 'WARNING') as logs, self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(self.document_url('pan_card'))
            self.storage_delete.assert_not_called()

        self.assertEqual(response.status_code, 200, response.data)
        # Who deleted it is kept in the server log, once the deletion has committed.
        self.assertEqual(logs.output, [
            f'WARNING:apps.works.views:Work {self.work.pk}: pan_card document deleted by user {self.admin.pk}',
        ])
        item = checklist_item(response.data, 'pan_card')
        self.assertEqual((item['provided'], item['document']), (False, None))
        self.assertEqual(response.data['work']['document_summary']['missing_count'], 10)
        self.assertFalse(WorkDocument.objects.exists())
        self.storage_delete.assert_called_once_with([('raw', document.cloudinary_public_id)])

    def test_a_work_never_serves_or_deletes_another_works_document(self):
        other = convert_lead(self.plan, self.admin, phone='9876500002')
        document = add_document(self.work, 'pan_card', self.admin)
        self.client.force_authenticate(self.admin)

        with self.captureOnCommitCallbacks(execute=True):
            opened = self.client.get(self.file_url('pan_card', other))
            deleted = self.client.delete(self.document_url('pan_card', other))
        listed = self.client.get(self.documents_url(other))

        self.assertEqual((opened.status_code, deleted.status_code), (404, 404))
        self.assertFalse(checklist_item(listed.data, 'pan_card')['provided'])
        self.assertTrue(WorkDocument.objects.filter(pk=document.pk, work=self.work).exists())
        self.assert_storage_untouched()

    def test_a_missing_work_or_a_document_never_uploaded_is_not_found(self):
        self.client.force_authenticate(self.admin)

        self.assertEqual(self.statuses(SimpleNamespace(pk=999999)), dict.fromkeys(ENDPOINTS, 404))
        self.assertEqual(self.client.get(self.file_url('pan_card')).status_code, 404)
        self.assertEqual(self.client.delete(self.document_url('pan_card')).status_code, 404)
        self.assert_storage_untouched()


class DocumentSummaryListTests(DocumentTestCase):
    """Each Work's document_summary, on the list (the Works page, the Pipeline board) and the Work itself."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def test_every_listed_work_reports_its_own_documents(self):
        other = convert_lead(self.plan, self.admin, name='Biju Paul', phone='9876500002')  # no email
        for key in ('bank_passbook', 'aadhar_card', 'pan_card'):
            add_document(self.work, key, self.admin)
        add_document(other, 'dcr', self.admin)

        # As the Works page asks for them, and the Pipeline board for one stage's column.
        for params in ({}, {'stage': WorkStage.LOAN_DOCUMENTS, 'page_size': 25}):
            with self.subTest(params=params):
                response = self.client.get(reverse('work-list'), params)

                self.assertEqual(response.status_code, 200, response.data)
                summaries = {row['id']: row['document_summary'] for row in response.data['results']}
                self.assertEqual(set(summaries), {self.work.pk, other.pk})
                mine, theirs = summaries[self.work.pk], summaries[other.pk]
                self.assertEqual((mine['completed_count'], mine['missing_count']), (5, 7))
                self.assertEqual(mine['missing_documents'], FILE_NAMES[3:])
                self.assertEqual((theirs['completed_count'], theirs['missing_count']), (2, 10))
                self.assertIn('Email ID', theirs['missing_documents'])
                self.assertNotIn('DCR', theirs['missing_documents'])

        detail = self.client.get(reverse('work-detail', args=[self.work.pk]))
        self.assertEqual(detail.data['document_summary'], mine)

    def test_documents_never_multiply_activity_counts_or_list_rows(self):
        for description in ('Site visit.', 'Customer call.', 'Panels delivered.'):
            Activity.objects.create(work=self.work, type='NOTE', description=description, created_by=self.admin)
        for key in FILE_KEYS[:4]:
            add_document(self.work, key, self.admin)
        other = convert_lead(self.plan, self.admin, phone='9876500002')

        listed = self.client.get(reverse('work-list')).data
        pages = [self.client.get(reverse('work-list'), {'page_size': 1, 'page': page}).data for page in (1, 2)]

        self.assertEqual(listed['count'], 2)
        self.assertCountEqual([row['id'] for row in listed['results']], [self.work.pk, other.pk])
        row = next(row for row in listed['results'] if row['id'] == self.work.pk)
        self.assertEqual((row['activity_count'], row['pending_activity_count']), (3, 3))
        self.assertEqual(row['document_summary']['completed_count'], 6)
        self.assertEqual([page['count'] for page in pages], [2, 2])
        self.assertCountEqual([page['results'][0]['id'] for page in pages], [self.work.pk, other.pk])

    def test_the_list_takes_as_many_queries_for_five_works_with_documents_as_for_one(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(reverse('work-list')).status_code, 200)
            return len(captured)

        add_document(self.work, 'pan_card', self.admin)
        one = queries()
        for n in range(4):
            work = convert_lead(self.plan, self.admin, phone=f'987650000{n}')
            for key in FILE_KEYS[:3]:
                add_document(work, key, self.admin)

        self.assertEqual(queries(), one)


class DocumentCompletenessTests(DocumentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.staff_with_work_module())

    def summary(self, work):
        return self.client.get(reverse('work-detail', args=[work.pk])).data['document_summary']

    def test_a_work_converted_without_an_email_also_misses_its_email_id(self):
        without = convert_lead(self.plan, self.admin, phone='9876500002')

        self.assertEqual(self.summary(without)['missing_count'], 11)
        self.assertEqual(self.summary(without)['missing_documents'], FILE_NAMES[:4] + ['Email ID'] + FILE_NAMES[4:])
        self.assertEqual(self.summary(self.work)['missing_count'], 10)

    def test_a_work_is_complete_once_all_ten_files_are_uploaded_and_stays_in_its_stage(self):
        for key in FILE_KEYS:
            response = self.upload(key, f'{key}.pdf')
            self.assertEqual(response.status_code, 200, response.data)

        self.assertEqual(response.data['work']['document_summary'], {
            'required_count': 12, 'completed_count': 12, 'missing_count': 0, 'is_complete': True,
            'missing_documents': [],
        })
        self.assertTrue(all(item['provided'] for group in response.data['groups'] for item in group['items']))
        # Documents never move a Work along the pipeline.
        self.assertEqual(Work.objects.get(pk=self.work.pk).stage, WorkStage.LOAN_DOCUMENTS)

    def test_the_email_id_is_the_works_email_which_can_be_added_later(self):
        without = convert_lead(self.plan, self.admin, phone='9876500002')
        url = reverse('work-detail', args=[without.pk])

        added = self.client.patch(url, {'email': 'a@b.com'}, format='json')
        self.assertEqual(added.status_code, 200, added.data)
        self.assertEqual(added.data['document_summary']['missing_count'], 10)
        self.assertTrue(checklist_item(self.client.get(self.documents_url(without)).data, 'email_id')['provided'])

        invalid = self.client.patch(url, {'email': 'not-an-email'}, format='json')
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('email', invalid.data)

        # The customer's other details stay the record from conversion.
        ignored = self.client.patch(url, {'phone': '123', 'customer_name': 'X'}, format='json')
        self.assertEqual(ignored.status_code, 200, ignored.data)
        work = Work.objects.get(pk=without.pk)
        self.assertEqual((work.phone, work.customer_name, work.email), ('9876500002', 'Asha Menon', 'a@b.com'))

        cleared = self.client.patch(url, {'email': ''}, format='json')
        self.assertIn('Email ID', cleared.data['document_summary']['missing_documents'])


class DocumentDeletionTests(DocumentTestCase):
    """However records go, their files go too, and only once that has committed."""

    def test_bulk_deleting_a_work_deletes_its_documents_and_then_their_files(self):
        removed = [add_document(self.work, key, self.admin) for key in ('pan_card', 'dcr', 'gps_photo')]
        other = convert_lead(self.plan, self.admin, phone='9876500002')
        kept = add_document(other, 'pan_card', self.admin)
        self.client.force_authenticate(self.admin)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('work-bulk-delete'), {'ids': [self.work.pk]}, format='json')
            self.storage_delete.assert_not_called()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['succeeded'], response.data['failed']), ([self.work.pk], []))
        self.assertFalse(Work.objects.filter(pk=self.work.pk).exists())
        self.assertEqual(list(WorkDocument.objects.all()), [kept])
        self.storage_delete.assert_called_once()
        self.assertCountEqual(
            self.storage_delete.call_args.args[0], [('raw', document.cloudinary_public_id) for document in removed],
        )

    def test_bulk_deleting_several_works_deletes_all_their_files_in_one_go(self):
        works = [self.work] + [convert_lead(self.plan, self.admin, phone=f'98765000{n}') for n in (10, 11)]
        files = [
            ('raw', add_document(work, key, self.admin).cloudinary_public_id)
            for work in works for key in ('pan_card', 'dcr')
        ]
        self.client.force_authenticate(self.admin)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('work-bulk-delete'), {'ids': [work.pk for work in works]}, format='json',
            )

        self.assertEqual(response.data['succeeded'], sorted(work.pk for work in works))
        # One call for every Work's files, rather than one per Work: storage.delete batches them 100 per request.
        self.storage_delete.assert_called_once()
        self.assertCountEqual(self.storage_delete.call_args.args[0], files)

    def test_the_manager_offers_no_delete_that_would_empty_the_table(self):
        add_document(self.work, 'pan_card', self.admin)

        with self.assertRaises(AttributeError):
            WorkDocument.objects.delete()
        self.assertEqual(WorkDocument.objects.count(), 1)

    def test_resetting_the_crm_deletes_every_document_and_then_their_files(self):
        other = convert_lead(self.plan, self.admin, phone='9876500002')
        removed = [add_document(self.work, 'pan_card', self.admin), add_document(other, 'dcr', self.staff)]

        with self.captureOnCommitCallbacks(execute=True), self.assertLogs('apps.maintenance.reset', 'WARNING'):
            counts = reset_crm_data(self.admin)
            self.storage_delete.assert_not_called()

        # What the reset reports and previews is unchanged: documents go with their Works.
        self.assertEqual(set(counts), {'leads', 'works', 'activities', 'notifications'})
        self.assertEqual(set(crm_record_counts()), set(counts))
        self.assertEqual((counts['works'], Work.objects.count(), WorkDocument.objects.count()), (2, 0, 0))
        self.storage_delete.assert_called_once()
        self.assertCountEqual(
            self.storage_delete.call_args.args[0], [('raw', document.cloudinary_public_id) for document in removed],
        )

    def test_deleting_works_through_a_queryset_is_refused_while_they_have_documents(self):
        add_document(self.work, 'pan_card', self.admin)

        # The safety net: a deletion that skips Work.delete() can't leave the files behind.
        with self.assertRaises(ProtectedError), transaction.atomic():
            Work.objects.filter(pk=self.work.pk).delete()

        self.assertEqual((Work.objects.filter(pk=self.work.pk).count(), WorkDocument.objects.count()), (1, 1))
        self.storage_delete.assert_not_called()

    def test_deleting_a_document_record_deletes_its_file_once_that_commits(self):
        document = add_document(self.work, 'pan_card', self.admin)

        with self.captureOnCommitCallbacks(execute=True):
            WorkDocument.objects.get(pk=document.pk).delete()
            self.storage_delete.assert_not_called()

        self.storage_delete.assert_called_once_with([('raw', document.cloudinary_public_id)])
        self.assertFalse(WorkDocument.objects.exists())

    def test_the_database_keeps_one_document_per_key_and_file_keys_only(self):
        existing = add_document(self.work, 'pan_card', self.admin)

        for key, fields in [
            ('pan_card', {}),  # a second PAN card
            ('email_id', {}),  # a Work field, never a file
            ('passport', {}),
            ('dcr', {'cloudinary_public_id': existing.cloudinary_public_id}),  # a file already on record
        ]:
            with self.subTest(key=key, **fields), self.assertRaises(IntegrityError), transaction.atomic():
                add_document(self.work, key, self.admin, **fields)


@override_settings(**{**CLOUDINARY, 'CLOUDINARY_API_SECRET': 'super-secret-value-123'})
class SecretHygieneTests(DocumentTestCase):
    """The real storage.py against a stand-in Cloudinary: the API secret never reaches an answer or the server log."""

    SECRET = 'super-secret-value-123'

    def setUp(self):
        # Instead of DocumentTestCase's stand-ins for storage.py: only urlopen is replaced, so the real client runs.
        self.cloudinary = FakeCloudinary(self.SECRET)
        patcher = mock.patch(URLOPEN, self.cloudinary)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_secret_never_reaches_an_answer_or_the_log(self):
        self.client.force_authenticate(self.admin)

        with self.assertLogs(level='DEBUG') as logs:
            responses = [
                self.upload('pan_card', 'pan.pdf', PDF),
                self.client.get(reverse('work-list')),
                self.client.get(reverse('work-detail', args=[self.work.pk])),
                self.client.get(self.documents_url()),
                self.client.get(self.file_url('pan_card')),
            ]
            with self.captureOnCommitCallbacks(execute=True):  # the replaced file is deleted
                responses.append(self.upload('pan_card', 'pan.png', PNG))
            with self.captureOnCommitCallbacks(execute=True):
                responses.append(self.client.delete(self.document_url('pan_card')))
            responses.append(self.upload('dcr', 'dcr.pdf', PDF))
            dcr = WorkDocument.objects.get(document_key='dcr').cloudinary_public_id

            # Cloudinary refusing every request, as after the secret is rotated there: each failure is logged.
            self.cloudinary.secret = 'rotated-secret'
            with self.assertLogs('django.request', 'ERROR') as request_logs:
                failed = [self.upload('gps_photo'), self.client.get(self.file_url('dcr'))]
            with self.captureOnCommitCallbacks(execute=True):
                responses.append(self.client.delete(self.document_url('dcr')))

        self.assertEqual([response.status_code for response in responses], [200] * 9)
        self.assertEqual([response.status_code for response in failed], [502, 502])
        # Both PAN card files are gone; the DCR's file couldn't be deleted, and the log says which it is.
        self.assertEqual(set(self.cloudinary.files), {dcr})
        log = '\n'.join(logs.output + request_logs.output)
        self.assertIn('Invalid Signature', log)
        self.assertIn(dcr, log)
        self.assertNotIn(self.SECRET, log)

        bodies = [b''.join(r.streaming_content) if r.streaming else r.content for r in responses + failed]
        self.assertEqual(bodies[4], PDF)
        uploaded = [upload_fields(r)['public_id'] for r in self.cloudinary.requests if r.full_url.endswith('/upload')]
        for response, body in zip(responses + failed, bodies):
            sent = body + str(list(response.items())).encode()
            self.assertNotIn(self.SECRET.encode(), sent)
            self.assertNotIn(b'api_key', sent)
            self.assertNotIn(b'cloudinary', sent.lower())
            for value in self.cloudinary.signatures + uploaded:
                self.assertNotIn(value.encode(), sent)
        # Cloudinary itself gets the secret only inside the Admin API's Basic auth, never in a URL or a body.
        for request in self.cloudinary.requests:
            self.assertNotIn(self.SECRET, request.full_url)
            self.assertNotIn(self.SECRET.encode(), request.data or b'')
