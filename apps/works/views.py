import logging
import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, Count, F, IntegerField, Min, Prefetch, Q, Sum, Value, When
from django.db.models.functions import Concat
from django.http import FileResponse
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.accounts.permissions import IsAdminRole
from apps.activities.models import ActivityStatus
from apps.leads.views import bulk_apply
from apps.notifications import services as notifications

from . import storage
from .documents import (
    ACCEPTED_EXTENSIONS, GROUPS, MAX_FILE_SIZE, REQUIREMENTS, TOO_LARGE, check_file, file_requirement, is_provided,
)
from .models import Work, WorkDocument, WorkStage
from .serializers import (
    BulkIdsSerializer, BulkStageSerializer, WorkDocumentSerializer, WorkQuerySerializer, WorkSerializer,
)

logger = logging.getLogger(__name__)

User = get_user_model()

# A document in a Work's URLs: /works/{id}/documents/{key}/. The key, never an id, so a document is only ever reached
# through its own Work.
DOCUMENT_PATH = r'documents/(?P<key>[a-z_]+)'
UPLOAD_FAILED = 'Document upload failed. Please try again.'
FILE_UNAVAILABLE = "The document couldn't be loaded. Please try again."

# Sorting by stage follows the pipeline (Loan Work first, Completed last) rather than the alphabet.
STAGE_RANK = Case(
    *(When(stage=value, then=Value(rank)) for rank, value in enumerate(WorkStage.values)),
    output_field=IntegerField(),
)

_pending = Q(activities__status=ActivityStatus.PENDING)
# Each Work's activities in brief, in the same query as the Works: no request per card.
ACTIVITY_STATS = {
    'activity_count': Count('activities'),
    'pending_activity_count': Count('activities', filter=_pending),
    'next_activity_due': Min('activities__due_date', filter=_pending),
}


class CanUseWorks(BasePermission):
    """The Work module, given to a role in Settings → Roles & Access: viewing Works and moving or updating them."""

    def has_permission(self, request, view):
        return request.user.has_perm('accounts.access_work')


class WorkPagination(PageNumberPagination):
    page_size_query_param = 'page_size'
    max_page_size = 100


def filter_works(works, query_params):
    query = WorkQuerySerializer(data=query_params)
    query.is_valid(raise_exception=True)
    params = query.validated_data

    if search := params.get('search', '').strip():
        match = (
            Q(customer_name__icontains=search) | Q(email__icontains=search) | Q(area__icontains=search)
            | Q(district__icontains=search) | Q(pin_code__icontains=search)
        )
        # A phone-like search matches the number with or without its country code, ignoring spaces and a leading 0.
        if re.fullmatch(r'[0-9\s()+-]+', search):
            digits = re.sub(r'[^0-9]', '', search).lstrip('0')
            if digits:
                match |= Q(full_phone__contains=digits)
        # "12" and "#12" also find Work 12.
        work_id = search.removeprefix('#')
        if re.fullmatch(r'[0-9]{1,18}', work_id):
            match |= Q(pk=int(work_id))
        works = works.annotate(full_phone=Concat('country_code', 'phone')).filter(match)

    for field in ('stage', 'assigned_to', 'plan'):
        if field in params:
            works = works.filter(**{field: params[field]})
    # Calendar days in the CRM's time zone (Asia/Kolkata), both ends included.
    if 'created_after' in params:
        works = works.filter(created_at__date__gte=params['created_after'])
    if 'created_before' in params:
        works = works.filter(created_at__date__lte=params['created_before'])

    ordering = params.get('ordering', '-created_at')
    field = F('stage_rank' if ordering.lstrip('-') == 'stage' else ordering.lstrip('-'))
    order = field.desc(nulls_last=True) if ordering.startswith('-') else field.asc(nulls_last=True)
    # Pinned Works come first; the id keeps pages stable when many Works share the sorted value.
    return works.annotate(stage_rank=STAGE_RANK).order_by('-is_pinned', order, '-id')


class WorkViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    # No create, and no single delete: converting a lead creates its Work, and a Work keeps the job's history. An admin
    # can remove Works in bulk from the list (bulk_delete); POST is for the bulk actions and document uploads. No PUT:
    # only the stage, assignee, due date, pin and email change. DELETE is for a document only (an admin's).
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']
    serializer_class = WorkSerializer
    permission_classes = [CanUseWorks]
    pagination_class = WorkPagination
    lookup_value_regex = '[0-9]+'

    def get_queryset(self):
        works = Work.objects.select_related('plan', 'assigned_to')
        if self.action in ('list', 'summary'):
            works = filter_works(works, self.request.query_params)
        # Not for the summary: joining the activities would count each Work's amount once per activity.
        if self.action == 'summary':
            return works
        # The documents for each Work's document_summary: one more query for the whole page, never one per Work, and no
        # join, so the activity counts and the paging are unaffected.
        documents = Prefetch('documents', queryset=WorkDocument.objects.select_related('uploaded_by'))
        return works.annotate(**ACTIVITY_STATS).prefetch_related(documents)

    def get_permissions(self):
        # Deleting Works (with their activity history) or a document is an admin's decision; the Work module covers the
        # rest, documents included.
        return [IsAdminRole()] if self.action in ('bulk_delete', 'delete_document') else [CanUseWorks()]

    def set_stage(self, work, stage):
        """Moves the Work and, when it reaches Completed, tells the admins. Called for a PATCH and for the bulk action."""
        was_completed = work.stage == WorkStage.COMPLETED
        if work.stage != stage:
            work.stage = stage
            work.save(update_fields=['stage', 'updated_at'])
        if stage == WorkStage.COMPLETED and not was_completed:
            notifications.work_completed(work, self.request.user)

    def perform_update(self, serializer):
        before = serializer.instance.stage
        work = serializer.save()
        if work.stage == WorkStage.COMPLETED and before != WorkStage.COMPLETED:
            notifications.work_completed(work, self.request.user)

    @action(detail=False, methods=['post'], url_path='bulk-stage')
    def bulk_stage(self, request):
        """Moves the selected Works to one stage, as the row's stage control does for one."""
        body = BulkStageSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        stage = body.validated_data['stage']
        return bulk_apply(
            Work.objects.all(), body.validated_data['ids'], lambda work: self.set_stage(work, stage),
            label=lambda work: work.customer_name,
        )

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        """Admins only. Removes the selected Works and their activities; each lead stays Won and can be converted again."""
        body = BulkIdsSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        files = []
        response = bulk_apply(
            Work.objects.all(), body.validated_data['ids'], lambda work: work.delete(files=files),
            label=lambda work: work.customer_name,
        )
        # After the Works' own commit hooks, which fill `files`: their documents' files in one go, 100 per Cloudinary
        # request, rather than a request per Work.
        transaction.on_commit(lambda: storage.delete(files), robust=True)
        return response

    @action(detail=False)
    def summary(self, request):
        """Every stage with its number of Works and their total confirmed amount, for the same search and filters."""
        rows = self.get_queryset().order_by().values('stage').annotate(count=Count('id'), total=Sum('amount'))
        totals = {row['stage']: row for row in rows}
        return Response([
            {
                'stage': stage,
                'label': label,
                'count': totals.get(stage, {}).get('count', 0),
                # Money is a two-decimal string, as the Work's own amount is, whatever the database returns for a sum.
                'total_amount': str((totals.get(stage, {}).get('total') or Decimal(0)).quantize(Decimal('0.01'))),
            }
            for stage, label in WorkStage.choices
        ])

    @action(detail=False)
    def assignees(self, request):
        """The active users a Work can be assigned to: only ids and names."""
        return Response(list(User.objects.filter(is_active=True).order_by('name', 'id').values('id', 'name')))

    # A Work's documents: what the Documents page shows and does. Every document is reached through its Work (the key
    # is looked up within the Work in the URL), after the same access check as the Work itself.

    def document_checklist(self, work):
        """The Documents page: the Work (with its document_summary) and every required document, in groups, uploaded or
        not."""
        uploaded = {document.document_key: document for document in work.documents.all()}

        def item(requirement):
            document = uploaded.get(requirement.key)
            return {
                'key': requirement.key,
                'name': requirement.name,
                'required': requirement.required,
                # A file to upload, or a field of the Work itself (its email or phone).
                'kind': 'field' if requirement.field else 'file',
                'field': requirement.field or None,
                'provided': is_provided(requirement, work, uploaded),
                'accept': [] if requirement.field else ACCEPTED_EXTENSIONS,
                'document': WorkDocumentSerializer(document).data if document else None,
            }

        return {
            'work': self.get_serializer(work).data,
            'groups': [
                {'key': group, 'label': label, 'items': [item(r) for r in REQUIREMENTS if r.group == group]}
                for group, label in GROUPS
            ],
            'max_file_size': MAX_FILE_SIZE,
            'can_delete': IsAdminRole().has_permission(self.request, self),
        }

    @action(detail=True)
    def documents(self, request, pk=None):
        """Every document the Work needs, uploaded or not, and how complete they are."""
        return Response(self.document_checklist(self.get_object()))

    @action(detail=True, methods=['post'], url_path=DOCUMENT_PATH, parser_classes=[MultiPartParser])
    def upload_document(self, request, pk=None, key=None):
        """Uploads a document's file (multipart, as `file`), or replaces the one there: the old file is deleted only
        once the new one is stored and recorded. Answers the updated checklist."""
        work = self.get_object()
        file_requirement(key)
        # Refused before the body is read, so an oversized request never fills a temporary file. The margin is the
        # multipart framing around the file.
        length = request.META.get('CONTENT_LENGTH') or ''
        if length.isdigit() and int(length) > MAX_FILE_SIZE + 64 * 1024:
            raise ValidationError({'file': [TOO_LARGE]})
        upload = request.FILES.get('file')
        content_type, extension = check_file(upload)
        content = upload.read()

        try:
            public_id = storage.upload(content, f'ragno/works/{work.pk}/{key}', extension)
        except storage.StorageError as error:
            logger.error('Work %s: uploading the %s document failed: %s', work.pk, key, error)
            return Response({'detail': UPLOAD_FAILED}, status=status.HTTP_502_BAD_GATEWAY)
        try:
            with transaction.atomic():
                replaced = WorkDocument.save_upload(
                    work, key, request.user, public_id=public_id, resource_type=storage.RESOURCE_TYPE,
                    filename=upload.name, content_type=content_type, size=len(content),
                )
        except Exception:
            # Not recorded, so nothing points at the new file.
            storage.delete([(storage.RESOURCE_TYPE, public_id)])
            raise
        # ponytail: a commit that fails after this point leaves the new file unreferenced (still private); sweep
        # ragno/works/ against the table if that ever matters.
        if replaced:
            # The replaced file is gone for good, so who replaced it goes to the server log (as a CRM reset does), once
            # the replacement has committed.
            transaction.on_commit(lambda: storage.delete([replaced]), robust=True)
            transaction.on_commit(lambda: logger.warning(
                'Work %s: %s document replaced by user %s (old file %s)', work.pk, key, request.user.pk, replaced[1],
            ))
        return Response(self.document_checklist(self.get_object()))

    @upload_document.mapping.delete
    def delete_document(self, request, pk=None, key=None):
        """Admins only. Deletes a document: its record now, its file once that commits."""
        work = self.get_object()
        # Locked first, as an upload locks it, so the record deleted is the one there once a running upload is done.
        Work.objects.select_for_update().get(pk=work.pk)
        if not work.documents.filter(document_key=key).delete()[0]:
            raise NotFound('This document has not been uploaded.')
        transaction.on_commit(
            lambda: logger.warning('Work %s: %s document deleted by user %s', work.pk, key, request.user.pk),
        )
        return Response(self.document_checklist(self.get_object()))

    @action(detail=True, url_path=f'{DOCUMENT_PATH}/file')
    def document_file(self, request, pk=None, key=None):
        """The document's file, passed on from Cloudinary to someone who may open the Work: the browser never learns
        where it is kept, and nothing is cached."""
        document = self.get_object().documents.filter(document_key=key).first()
        if document is None:
            raise NotFound('This document has not been uploaded.')
        try:
            stored = storage.fetch(document.cloudinary_public_id, document.cloudinary_resource_type)
        except storage.StorageError as error:
            logger.error('Work %s: fetching the %s document failed: %s', document.work_id, key, error)
            return Response({'detail': FILE_UNAVAILABLE}, status=status.HTTP_502_BAD_GATEWAY)
        response = FileResponse(stored, content_type=document.content_type, filename=document.original_filename)
        response['Cache-Control'] = 'private, no-store'
        return response
