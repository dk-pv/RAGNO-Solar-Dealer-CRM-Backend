import csv

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, F, IntegerField, ProtectedError, Value, When
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import Role
from apps.accounts.permissions import IsAdminRole, ModelPermissions
from apps.notifications import services as notifications

from .models import Lead, LeadConflict, LeadStatus, SolarPlan
from .serializers import (
    BulkIdsSerializer, BulkStatusSerializer, LeadQuerySerializer, LeadSerializer, SolarPlanSerializer,
    StatusChangeSerializer,
)

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

# Sorting by status follows the pipeline (New first, Lost last) rather than the alphabet.
STATUS_RANK = Case(
    *(When(status=value, then=Value(rank)) for rank, value in enumerate(LeadStatus.values)),
    output_field=IntegerField(),
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
    if 'assigned_to' in params:
        leads = leads.filter(assigned_to_id=params['assigned_to'])
    # Calendar days in the CRM's time zone (Asia/Kolkata), both ends included.
    if 'created_after' in params:
        leads = leads.filter(created_at__date__gte=params['created_after'])
    if 'created_before' in params:
        leads = leads.filter(created_at__date__lte=params['created_before'])

    ordering = params.get('ordering', '-created_at')
    field = F('status_rank' if ordering.lstrip('-') == 'status' else ordering.lstrip('-'))
    order = field.desc(nulls_last=True) if ordering.startswith('-') else field.asc(nulls_last=True)
    # Pinned leads come first; the id keeps pages stable when many leads share the sorted value.
    return leads.annotate(status_rank=STATUS_RANK).order_by('-is_pinned', order, '-id')


def csv_cell(value):
    """Text starting with = + - @ is kept as text rather than run as a formula when a spreadsheet opens the file."""
    text = str(value)
    return f"'{text}" if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


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
        leads = Lead.objects.visible_to(self.request.user).select_related('plan', 'assigned_to', 'created_by', 'work')
        if self.action in ('list', 'export'):
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
        if self.action == 'assignees':
            return [CanViewLeads()]
        return [ModelPermissions()]

    def perform_create(self, serializer):
        user = self.request.user
        # A lead staff add is theirs: assigned to them, so it stays in their list. Admins assign anyone.
        extra = {} if user.role_id == Role.ADMIN else {'assigned_to': user}
        lead = serializer.save(created_by=user, **extra)
        notifications.lead_created(lead, user)
        # The initial follow-up added with the lead, if any, has its own assignee.
        for follow_up in lead.activities.select_related('assigned_to', 'lead'):
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
            'Plan', 'Amount', 'Status', 'Source', 'Assigned to', 'Next follow-up', 'Created',
        ])
        for lead in self.get_queryset():
            writer.writerow([csv_cell(value) for value in (
                lead.id, lead.name, lead.country_code, lead.phone, lead.email, lead.area, lead.district, lead.state,
                lead.pin_code, lead.plan.name, lead.amount, lead.get_status_display(), lead.get_source_display(),
                lead.assigned_to.name if lead.assigned_to else '', lead.next_follow_up or '',
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


class SolarPlanListView(ListAPIView):
    """Every plan with its current price, for the lead form and filters. Admins manage prices."""

    queryset = SolarPlan.objects.all()
    serializer_class = SolarPlanSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None
