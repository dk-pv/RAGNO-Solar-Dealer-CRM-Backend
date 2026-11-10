import csv
import logging
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, F, OuterRef, ProtectedError, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import Role, by_name
from apps.accounts.permissions import IsAdminRole, ModelPermissions
from apps.notifications import services as notifications

from apps.works.models import Work

from .models import (
    FIRST_STAGE, LAST_STAGES, Lead, LeadConflict, Pipeline, PipelineStage, SolarPlan, stage_choices, stage_name_of,
    stage_rank,
)
from .serializers import (
    BulkIdsSerializer, BulkStatusSerializer, LeadQuerySerializer, LeadSerializer, PipelineStageSerializer,
    SolarPlanSerializer, StatusChangeSerializer,
)

logger = logging.getLogger(__name__)

PROTECTED_LEAD = "This lead has records that depend on it, such as its Work, so it can't be deleted."
MISSING_RECORD = "This record no longer exists, or you don't have access to it."


def bulk_apply(records, ids, apply, label=str):
    """Runs `apply(record)` on each of the user's `records` whose id is in `ids`, each in its own savepoint, so one
    refusal (a LeadConflict, a ProtectedError) rolls back only that record and the rest still go through. Answers which
    ids succeeded and, for each that didn't, its `label` and why, in words meant for the screen. An id outside `records`
    (gone, or another person's) fails as not found: a bulk action never reaches further than the single-record actions."""
    found = {record.pk: record for record in records.filter(pk__in=ids)}
    succeeded, failed = [], []
    # Ascending id: a row lock (move_to, convert) lasts until the request commits, so two concurrent bulk actions on
    # the same rows take the locks in the same order and can't deadlock.
    for record_id in sorted(ids):
        record = found.get(record_id)
        if record is None:
            failed.append({'id': record_id, 'name': None, 'reason': MISSING_RECORD})
            continue
        try:
            with transaction.atomic():
                apply(record)
        except LeadConflict as refusal:
            failed.append({'id': record_id, 'name': label(record), 'reason': str(refusal.detail)})
        except ProtectedError:
            failed.append({'id': record_id, 'name': label(record), 'reason': PROTECTED_LEAD})
        else:
            succeeded.append(record_id)  # not record.pk: a deleted record's pk is cleared
    return Response({'succeeded': succeeded, 'failed': failed})

User = get_user_model()

# Sorting by status follows the pipeline as the board shows it (New first, an admin's stages where they put them, Won and
# Lost last) rather than the alphabet.
STATUS_RANK = stage_rank('status')
# Sorting by assignee: by the first one's name, as a subquery, so a lead with several assignees is still one row.
FIRST_ASSIGNEE_NAME = Subquery(
    User.objects.filter(assigned_leads=OuterRef('pk')).order_by('name', 'pk').values('name')[:1],
)


def filter_leads(leads, query_params):
    query = LeadQuerySerializer(data=query_params)
    query.is_valid(raise_exception=True)
    params = query.validated_data

    leads = leads.search(params.get('search', ''))

    for field in ('status', 'source'):
        if field in params:
            leads = leads.filter(**{field: params[field]})
    if 'plan' in params:
        leads = leads.filter(plan_id=params['plan'])
    if 'work_type' in params:
        leads = leads.filter(work_type='' if params['work_type'] == 'none' else params['work_type'])
    # The leads this user is one of the assignees of (one row per lead).
    if 'assigned_to' in params:
        leads = leads.filter(assignees=params['assigned_to'])
    # Calendar days in the CRM's time zone (Asia/Kolkata), both ends included.
    if 'created_after' in params:
        leads = leads.filter(created_at__date__gte=params['created_after'])
    if 'created_before' in params:
        leads = leads.filter(created_at__date__lte=params['created_before'])

    ordering = params.get('ordering', '-created_at')
    key = ordering.lstrip('-')
    if key == 'assigned_to__name':
        leads = leads.annotate(first_assignee_name=FIRST_ASSIGNEE_NAME)
    field = F({'status': 'status_rank', 'assigned_to__name': 'first_assignee_name'}.get(key, key))
    order = field.desc(nulls_last=True) if ordering.startswith('-') else field.asc(nulls_last=True)
    # Pinned leads come first; the id keeps pages stable when many leads share the sorted value.
    return leads.annotate(status_rank=STATUS_RANK).order_by('-is_pinned', order, '-id')


def csv_cell(value):
    """Text starting with = + - @ is kept as text rather than run as a formula when a spreadsheet opens the file."""
    text = str(value)
    return f"'{text}" if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


def stage_summary(pipeline, totals, field):
    """Every stage of the pipeline in the board's order, with its number of records and their total amount: the board's
    column headers. `totals` maps a stage's key to {'count', 'total'}; `field` names the key ('status' or 'stage')."""
    return [
        {
            field: key,
            'label': name,
            'count': totals.get(key, {}).get('count', 0),
            # Money is a two-decimal string, as a record's own amount is, whatever the database returns for a sum.
            'total_amount': str((totals.get(key, {}).get('total') or Decimal(0)).quantize(Decimal('0.01'))),
        }
        for key, name in stage_choices(pipeline)
    ]


class CanViewLeads(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('leads.view_lead')


class CanChangeLeads(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('leads.change_lead')


class CanDeleteLeads(BasePermission):
    """The Leads module doesn't give staff the delete permission: in practice, admins."""

    def has_permission(self, request, view):
        return request.user.has_perm('leads.delete_lead')


class CanUsePlans(BasePermission):
    """The plans and their prices are for the Leads and Works screens: the Leads or the Work module."""

    def has_permission(self, request, view):
        return request.user.has_perm('leads.view_lead') or request.user.has_perm('accounts.access_work')


class LeadPagination(PageNumberPagination):
    page_size_query_param = 'page_size'
    max_page_size = 100


class LeadViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    # Who can do what: the role gives the permission (an admin has all; staff get the Leads module's view, add and
    # change, but not delete) and Lead.objects.visible_to() limits staff to the leads assigned to them. A lead outside
    # that set answers 404 for every action, so staff can't read, change, convert or even detect another person's lead.
    serializer_class = LeadSerializer
    pagination_class = LeadPagination
    lookup_value_regex = '[0-9]+'

    def get_queryset(self):
        leads = (
            Lead.objects.visible_to(self.request.user).select_related('plan', 'created_by', 'work')
            .prefetch_related('assignees')
        )
        if self.action in ('list', 'export', 'summary'):
            return filter_leads(leads, self.request.query_params)
        return leads

    def get_permissions(self):
        if self.action == 'export':
            # The whole customer list with phone numbers leaves the system: admins only.
            return [IsAdminRole()]
        if self.action in ('change_status', 'convert', 'bulk_status', 'bulk_convert'):
            return [CanChangeLeads()]
        if self.action == 'bulk_delete':
            return [CanDeleteLeads()]
        if self.action in ('assignees', 'summary'):
            return [CanViewLeads()]
        return [ModelPermissions()]

    @action(detail=False)
    def summary(self, request):
        """Every status with its number of leads and their total amount, for the list's search and filters: what the
        Lead Pipeline's columns show, as the Works summary does for its stages. Staff get their own leads' numbers."""
        rows = self.get_queryset().order_by().values('status').annotate(count=Count('id'), total=Sum('amount'))
        totals = {row['status']: row for row in rows}
        return Response(stage_summary(Pipeline.LEAD, totals, 'status'))

    def perform_create(self, serializer):
        user = self.request.user
        # A lead staff add is theirs: assigned to them, so it stays in their list. Admins assign anyone.
        extra = {} if user.role_id == Role.ADMIN else {'assignees': [user]}
        lead = serializer.save(created_by=user, **extra)
        notifications.lead_created(lead, user)
        # The initial follow-up added with the lead, if any, has its own assignees.
        for follow_up in lead.activities.select_related('lead').prefetch_related('assignees'):
            notifications.activity_assigned(follow_up, user)

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response({'detail': PROTECTED_LEAD}, status=status.HTTP_409_CONFLICT)

    def convert_and_notify(self, lead):
        lead.convert(self.request.user)
        notifications.lead_converted(lead, self.request.user)

    @action(detail=True, methods=['post'], url_path='status')
    def change_status(self, request, pk=None):
        lead = self.get_object()
        body = StatusChangeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        lead.move_to(body.validated_data['status'])
        return Response(self.get_serializer(lead).data)

    @action(detail=True, methods=['post'])
    def convert(self, request, pk=None):
        lead = self.get_object()
        self.convert_and_notify(lead)
        return Response(self.get_serializer(lead).data)

    # The bulk actions act on the rows selected in the list, each through the same rules as its single-record action
    # (Lead.move_to, Lead.convert, delete with its PROTECT keys), and answer per lead (see bulk_apply).

    @action(detail=False, methods=['post'], url_path='bulk-status')
    def bulk_status(self, request):
        body = BulkStatusSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        new_status = body.validated_data['status']
        return bulk_apply(self.get_queryset(), body.validated_data['ids'], lambda lead: lead.move_to(new_status))

    @action(detail=False, methods=['post'], url_path='bulk-convert')
    def bulk_convert(self, request):
        body = BulkIdsSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return bulk_apply(self.get_queryset(), body.validated_data['ids'], self.convert_and_notify)

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        body = BulkIdsSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return bulk_apply(self.get_queryset(), body.validated_data['ids'], lambda lead: lead.delete())

    @action(detail=False, methods=['get'])
    def export(self, request):
        """The leads matching the list's search, filters and order, as CSV, without paging."""
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="leads-{timezone.localdate():%Y-%m-%d}.csv"'
        response.write('﻿')  # Lets Excel read the file as UTF-8.
        writer = csv.writer(response)
        writer.writerow([
            'Lead ID', 'Customer', 'Country code', 'Phone', 'Email', 'Area', 'District', 'State', 'PIN code',
            'Dealer', 'Plan', 'Amount', 'Work type', 'Status', 'Source', 'Assigned to', 'Next follow-up', 'Created',
        ])
        for lead in self.get_queryset().annotate(stage_label=stage_name_of('status')):
            writer.writerow([csv_cell(value) for value in (
                lead.id, lead.name, lead.country_code, lead.phone, lead.email, lead.area, lead.district, lead.state,
                lead.pin_code, lead.dealer_name, lead.plan.name if lead.plan else '', '' if lead.amount is None else lead.amount,
                lead.get_work_type_display(), lead.get_status_display(), lead.get_source_display(),
                ', '.join(user.name for user in by_name(lead.assignees.all())), lead.next_follow_up or '',
                timezone.localtime(lead.created_at).strftime('%Y-%m-%d %H:%M'),
            )])
        return response

    @action(detail=False, methods=['get'])
    def assignees(self, request):
        """Who a lead can be assigned to, as ids and names: any active user for an admin, only themselves for staff
        (staff can't reassign leads)."""
        users = User.objects.filter(is_active=True)
        if request.user.role_id != Role.ADMIN:
            users = users.filter(pk=request.user.pk)
        return Response(list(users.order_by('name', 'id').values('id', 'name')))


class SolarPlanViewSet(
    mixins.ListModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet,
):
    """Every plan with its current price, for the lead and Work forms and filters (the Leads or the Work module). Admins
    manage them in Settings → Solar Plans: add a plan, change its name, capacity or price, deactivate or reactivate it
    (see SolarPlanSerializer). No delete: leads and Works keep their plan (PROTECT), so a plan is retired by
    deactivating it."""

    queryset = SolarPlan.objects.all()
    serializer_class = SolarPlanSerializer
    pagination_class = None
    lookup_value_regex = '[0-9]+'

    def get_permissions(self):
        return [CanUsePlans()] if self.action == 'list' else [IsAdminRole()]


def records_in(stage):
    """The leads or the Works in a stage."""
    return Lead.objects.filter(status=stage.key) if stage.pipeline == Pipeline.LEAD else Work.objects.filter(stage=stage.key)


def _count_in(model, field):
    return Coalesce(
        Subquery(model.objects.filter(**{field: OuterRef('key')}).order_by().values(field).annotate(n=Count('pk')).values('n')),
        0,
    )


# The leads or Works in each stage. A key is never both a lead status and a Work stage, so one of the two is always 0.
RECORD_COUNT = _count_in(Lead, 'status') + _count_in(Work, 'stage')


class StageMoveSerializer(serializers.Serializer):
    direction = serializers.ChoiceField(choices=['up', 'down'])


class PipelineStageViewSet(
    mixins.ListModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Both pipelines' stages in the boards' order (?pipeline=LEAD or WORK for one). Every signed-in user reads them, as
    every screen that shows a lead's status or a Work's stage names it. Only admins change them (Settings → Pipeline
    Stages): add a stage (placed just before Won, or Completed), rename or move one of their own, and delete one of their
    own, its records going to another stage first (destroy). System stages never change."""

    serializer_class = PipelineStageSerializer
    pagination_class = None
    # No PUT: a stage's name is all that changes.
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']
    lookup_value_regex = '[0-9]+'

    def get_permissions(self):
        return [IsAuthenticated()] if self.action == 'list' else [IsAdminRole()]

    def get_queryset(self):
        stages = PipelineStage.objects.filter(is_active=True)
        pipeline = self.request.query_params.get('pipeline')
        if self.action == 'list' and pipeline:
            if pipeline not in Pipeline.values:
                raise ValidationError({'pipeline': f'"{pipeline}" is not a valid choice.'})
            stages = stages.filter(pipeline=pipeline)
        # How many records each stage holds: for admins, who decide where they go before deleting it.
        if self.request.user.role_id == Role.ADMIN:
            stages = stages.annotate(record_count=RECORD_COUNT)
        return stages

    def pipeline_response(self, pipeline, status_code=status.HTTP_200_OK):
        """The pipeline's stages after a change, so Settings shows the new order in one go."""
        stages = self.get_queryset().filter(pipeline=pipeline)
        return Response(self.get_serializer(stages, many=True).data, status=status_code)

    def perform_create(self, serializer):
        pipeline = serializer.validated_data['pipeline']
        # Locked, so two admins adding stages at once number them one after the other.
        stages = list(PipelineStage.objects.select_for_update().filter(pipeline=pipeline))
        try:
            with transaction.atomic():
                # An opaque key: the name can change, the key stays (it is in every record and URL that uses the stage).
                stage = serializer.save(key=f'CUSTOM_{uuid.uuid4().hex[:8].upper()}', is_system=False, order=len(stages))
        except IntegrityError:
            # The same name, added by someone else a moment ago.
            raise ValidationError({'name': 'This pipeline already has a stage with this name.'}) from None
        # renumber() keeps the last stages last, so the new stage lands just before them.
        PipelineStage.renumber(pipeline, [*stages, stage])
        transaction.on_commit(lambda: logger.warning(
            'Pipeline stage %s "%s" (%s) added by user %s', stage.key, stage.name, pipeline, self.request.user.pk,
        ))

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return self.pipeline_response(serializer.instance.pipeline, status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        if serializer.instance.is_system:
            raise PermissionDenied("System stages can't be renamed.")
        serializer.save()

    @action(detail=True, methods=['post'])
    def move(self, request, pk=None):
        """Moves one of the admin's stages one place earlier ("up") or later ("down"), past any stage but the first and
        the last ones. Answers the pipeline's stages."""
        body = StageMoveSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        stage = self.get_object()
        if stage.is_system:
            raise PermissionDenied("System stages can't be moved.")
        stages = list(PipelineStage.objects.select_for_update().filter(pipeline=stage.pipeline))
        index = next(i for i, other in enumerate(stages) if other.pk == stage.pk)
        target = index - 1 if body.validated_data['direction'] == 'up' else index + 1
        fixed = [FIRST_STAGE[stage.pipeline], *LAST_STAGES[stage.pipeline]]
        if not 0 <= target < len(stages) or stages[target].key in fixed:
            raise ValidationError({'direction': f'{stage.name} is already as far as it can go that way.'})
        stages[index], stages[target] = stages[target], stages[index]
        PipelineStage.renumber(stage.pipeline, stages)
        return self.pipeline_response(stage.pipeline)

    def destroy(self, request, *args, **kwargs):
        """Deletes one of the admin's stages. One with records needs ?move_to=<key>: another open stage of the same
        pipeline (not Won, Lost or Completed), where its records go first, in the same transaction. Without it the answer
        is 409, with how many records the stage holds. Answers the pipeline's remaining stages."""
        stage = self.get_object()
        if stage.is_system:
            raise PermissionDenied("System stages can't be deleted.")
        move_to = request.query_params.get('move_to') or ''
        # Both stages locked, in id order (as every deletion locks them, so two never deadlock), and before their records
        # (as a record moving in locks its stage first: lock_stage). A record moving into this stage meanwhile commits
        # first, and is counted and moved below.
        locked = {
            other.pk: other
            for other in PipelineStage.objects.select_for_update().filter(
                Q(pk=stage.pk) | Q(pipeline=stage.pipeline, key=move_to, is_active=True),
            ).order_by('pk')
        }
        if stage.pk not in locked:
            raise NotFound('This stage no longer exists.')
        records = records_in(stage)
        count = records.count()
        destination = None
        if count:
            noun = ('lead' if count == 1 else 'leads') if stage.pipeline == Pipeline.LEAD else ('Work' if count == 1 else 'Works')
            if not move_to:
                return Response(
                    {
                        'detail': f'{count} {noun} {"is" if count == 1 else "are"} in {stage.name}. Choose the stage to '
                                  f'move {"it" if count == 1 else "them"} to before deleting it.',
                        'record_count': count,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            destination = next((other for other in locked.values() if other.pk != stage.pk), None)
            if destination is None or destination.key in LAST_STAGES[stage.pipeline]:
                raise ValidationError({'move_to': f'Choose another open stage of the {stage.get_pipeline_display()}.'})
            field = 'status' if stage.pipeline == Pipeline.LEAD else 'stage'
            records.update(**{field: destination.key, 'updated_at': timezone.now()})
        stage.delete()
        PipelineStage.renumber(stage.pipeline)
        transaction.on_commit(lambda: logger.warning(
            'Pipeline stage %s "%s" (%s) deleted by user %s; %s records moved to %s', stage.key, stage.name,
            stage.pipeline, request.user.pk, count, destination.key if destination else '-',
        ))
        return self.pipeline_response(stage.pipeline)
