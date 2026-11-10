"""Additional Work documents: any number per Work with a title, category and description, in the same private storage
and behind the same access rules as the required ones, and never counted as one of them. Cloudinary is never called
(DocumentTestCase replaces storage.py's functions)."""

from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.urls import reverse
from rest_framework import status

from apps.maintenance.reset import reset_crm_data

from . import documents, storage
from .models import Work, WorkDocument
from .test_documents import JPEG, PDF, DocumentTestCase, add_document
from .tests import convert_lead


class AdditionalDocumentTestCase(DocumentTestCase):
    def upload_url(self, work=None):
        return reverse('work-upload-additional-document', args=[(work or self.work).pk])

    def item_url(self, document, work=None):
        return reverse('work-delete-additional-document', args=[(work or self.work).pk, document.pk])

    def file_url_of(self, document, work=None):
        return reverse('work-additional-document-file', args=[(work or self.work).pk, document.pk])

    def add(self, title='Customer agreement', name='agreement.pdf', content=PDF, work=None, **fields):
        file = SimpleUploadedFile(name, content, content_type='application/octet-stream')
        return self.client.post(
            self.upload_url(work), {'file': file, 'title': title, **fields}, format='multipart',
        )

    def additional(self, title, work=None):
        return WorkDocument.objects.get(work=work or self.work, document_key=documents.ADDITIONAL, title=title)


class AdditionalUploadTests(AdditionalDocumentTestCase):
    def test_an_admin_or_a_user_with_the_work_module_uploads_documents_with_their_details(self):
        for user, title in ((self.admin, 'Quotation v2'), (self.staff_with_work_module(), 'Bank statement')):
            with self.subTest(user=user.name):
                self.client.force_authenticate(user)
                response = self.add(title=title, category='QUOTATION', description='Signed copy', name='q.jpg', content=JPEG)
                self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
                saved = self.additional(title)
                self.assertEqual(
                    (saved.category, saved.description, saved.content_type, saved.original_filename, saved.uploaded_by),
                    ('QUOTATION', 'Signed copy', 'image/jpeg', 'q.jpg', user),
                )
                self.assertTrue(saved.cloudinary_public_id.startswith(f'ragno/works/{self.work.pk}/additional/'))
                shown = next(item for item in response.data['additional'] if item['id'] == saved.pk)
                self.assertEqual((shown['title'], shown['category_label'], shown['uploaded_by_name']),
                                 (title, 'Quotation', user.name))
                # Where it is stored never reaches the browser.
                self.assertNotIn('cloudinary_public_id', shown)

    def test_any_number_per_work_newest_first_with_the_category_defaulting_to_other(self):
        self.client.force_authenticate(self.admin)
        self.add(title='First')
        response = self.add(title='Second')
        self.assertEqual([item['title'] for item in response.data['additional']], ['Second', 'First'])
        self.assertEqual(self.additional('First').category, 'OTHER')
        self.assertEqual(
            [category['value'] for category in response.data['additional_categories']],
            ['BANK', 'AGREEMENT', 'SITE_PHOTO', 'QUOTATION', 'INVOICE', 'OTHER'],
        )

    def test_each_document_belongs_to_the_work_it_was_uploaded_to(self):
        self.client.force_authenticate(self.admin)
        other = convert_lead(self.plan, self.admin, name='Ravi', phone='9876500001')
        self.add(title='Mine')
        self.add(title='Theirs', work=other)
        self.assertEqual(self.additional('Mine').work, self.work)
        listed = self.client.get(self.documents_url(other)).data['additional']
        self.assertEqual([item['title'] for item in listed], ['Theirs'])
        # Reached only through its own Work: the other Work's URL can't open, file or delete it.
        mine = self.additional('Mine')
        self.assertEqual(self.client.get(self.file_url_of(mine, work=other)).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(self.item_url(mine, work=other)).status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(WorkDocument.objects.filter(pk=mine.pk).exists())

    def test_a_required_document_is_never_reached_as_an_additional_one_nor_the_reverse(self):
        self.client.force_authenticate(self.admin)
        required = add_document(self.work, 'pan_card', self.admin)
        self.add(title='Extra')
        self.assertEqual(self.client.get(self.file_url_of(required)).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(self.item_url(required)).status_code, status.HTTP_404_NOT_FOUND)
        # The required documents' URLs refuse the additional documents' shared key.
        self.assertEqual(self.client.get(self.file_url(documents.ADDITIONAL)).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(self.document_url(documents.ADDITIONAL)).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(WorkDocument.objects.filter(work=self.work).count(), 2)


class AdditionalValidationTests(AdditionalDocumentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def test_wrong_types_mismatched_names_empty_and_oversized_files_are_refused_before_storage(self):
        oversized = PDF + bytes(documents.MAX_FILE_SIZE)
        for name, content in (('virus.exe', b'MZ\x90\x00' + bytes(20)), ('scan.pdf', JPEG), ('empty.pdf', b''),
                              ('big.pdf', oversized)):
            with self.subTest(name):
                response = self.add(name=name, content=content)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn('file', response.data)
        self.assertEqual(self.client.post(self.upload_url(), {'title': 'No file'}, format='multipart').status_code, 400)
        self.storage_upload.assert_not_called()
        self.assertFalse(WorkDocument.objects.exists())

    def test_the_title_is_required_and_the_category_must_be_one_of_the_list(self):
        for fields in ({'title': ''}, {'title': ' '}, {'title': 'x' * 151}, {'title': 'Ok', 'category': 'AADHAAR'},
                       {'title': 'Ok', 'description': 'x' * 1001}):
            with self.subTest(fields=fields):
                self.assertEqual(self.add(**fields).status_code, status.HTTP_400_BAD_REQUEST)
        self.storage_upload.assert_not_called()

    def test_a_storage_failure_saves_nothing_and_says_so_without_details(self):
        self.storage_upload.side_effect = storage.StorageError('Cloudinary answered 500: secret detail')
        with self.assertLogs('apps.works.views', 'ERROR'):
            response = self.add()
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data, {'detail': 'Document upload failed. Please try again.'})
        self.assertFalse(WorkDocument.objects.exists())

    def test_a_file_stored_but_not_recorded_is_deleted_again(self):
        public_id = f'ragno/works/{self.work.pk}/additional/stored.pdf'
        self.storage_upload.side_effect, self.storage_upload.return_value = None, public_id
        with mock.patch.object(WorkDocument, 'save_additional', side_effect=IntegrityError('could not save')):
            with self.assertRaises(IntegrityError):
                self.add()
        self.storage_delete.assert_called_once_with([(storage.RESOURCE_TYPE, public_id)])
        self.assertFalse(WorkDocument.objects.exists())


class AdditionalAccessTests(AdditionalDocumentTestCase):
    def test_users_without_the_work_module_can_do_nothing_with_them(self):
        self.client.force_authenticate(self.admin)
        self.add(title='Agreement')
        document = self.additional('Agreement')
        self.client.force_authenticate(self.staff)  # Activities-only staff, as the roles allow
        self.assertEqual(self.add(title='Mine').status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(self.file_url_of(document)).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.delete(self.item_url(document)).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(self.documents_url()).status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.file_url_of(document)).status_code, status.HTTP_401_UNAUTHORIZED)
        self.storage_fetch.assert_not_called()

    def test_only_admins_delete_them_and_the_file_goes_once_that_commits(self):
        self.client.force_authenticate(self.admin)
        self.add(title='Invoice')
        document = self.additional('Invoice')
        self.client.force_authenticate(self.staff_with_work_module())
        self.assertEqual(self.client.delete(self.item_url(document)).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.admin)
        with self.assertLogs('apps.works.views', 'WARNING'), self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(self.item_url(document))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['additional'], [])
        self.storage_delete.assert_called_once_with([(document.cloudinary_resource_type, document.cloudinary_public_id)])

    def test_the_file_is_sent_privately_through_the_api(self):
        self.client.force_authenticate(self.admin)
        self.add(title='Site photo 2', name='roof.pdf')
        document = self.additional('Site photo 2')
        self.client.force_authenticate(self.staff_with_work_module())

        response = self.client.get(self.file_url_of(document))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(b''.join(response.streaming_content), PDF)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertIn('roof.pdf', response['Content-Disposition'])
        self.storage_fetch.assert_called_once_with(document.cloudinary_public_id, document.cloudinary_resource_type)


class AdditionalCompletenessTests(AdditionalDocumentTestCase):
    def test_additional_documents_never_change_the_required_documents_completion(self):
        self.client.force_authenticate(self.admin)
        before = self.client.get(self.documents_url()).data['work']['document_summary']
        for category in ('BANK', 'SITE_PHOTO', 'OTHER'):
            response = self.add(title=f'Extra {category}', category=category)
        after = response.data['work']['document_summary']
        self.assertEqual(after, before)
        self.assertFalse(after['is_complete'])
        provided = [item['key'] for group in response.data['groups'] for item in group['items'] if item['provided']]
        self.assertEqual(provided, ['email_id', 'phone_number'])
        listed = self.client.get(reverse('work-detail', args=[self.work.pk])).data['document_summary']
        self.assertEqual(listed, before)

    def test_required_uploads_still_work_beside_additional_ones(self):
        self.client.force_authenticate(self.admin)
        self.add(title='Extra')
        self.assertEqual(self.upload('pan_card').status_code, status.HTTP_200_OK)
        self.assertEqual(self.upload('pan_card', name='again.pdf').status_code, status.HTTP_200_OK)  # replaces
        keys = sorted(WorkDocument.objects.filter(work=self.work).values_list('document_key', flat=True))
        self.assertEqual(keys, ['additional', 'pan_card'])

    def test_deleting_the_work_or_resetting_the_crm_removes_them_with_their_files(self):
        self.client.force_authenticate(self.admin)
        self.add(title='Extra')
        public_id = self.additional('Extra').cloudinary_public_id
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('work-bulk-delete'), {'ids': [self.work.pk]}, format='json')
        self.assertEqual(response.data['succeeded'], [self.work.pk])
        self.assertFalse(WorkDocument.objects.exists())
        self.assertIn((storage.RESOURCE_TYPE, public_id), self.storage_delete.call_args.args[0])

        work = convert_lead(self.plan, self.admin, name='Ravi', phone='9876500001')
        self.add(title='Again', work=work)
        with self.captureOnCommitCallbacks(execute=True):
            reset_crm_data(self.admin)
        self.assertFalse(Work.objects.exists() or WorkDocument.objects.exists())
