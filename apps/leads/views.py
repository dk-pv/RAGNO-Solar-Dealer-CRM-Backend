import csv
import re

from django.contrib.auth import get_user_model
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.db.models.functions import Concat
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdminRole, ModelPermissions

from .models import Lead, LeadStatus, SolarPlan
from .serializers import LeadQuerySerializer, LeadSerializer, SolarPlanSerializer, StatusChangeSerializer

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

    if search := params.get('search', '').strip():
        match = (
            Q(name__icontains=search) | Q(email__icontains=search) | Q(area__icontains=search)
            | Q(district__icontains=search) | Q(pin_code__icontains=search)
        )
        # A phone-like search matches the number with or without its country code, ignoring spaces and a leading 0.
        if re.fullmatch(r'[0-9\s()+-]+', search):
            digits = re.sub(r'[^0-9]', '', search).lstrip('0')
            if digits:
                match |= Q(full_phone__contains=digits)
        # "1024" and "#1024" also find lead 1024.
        lead_id = search.removeprefix('#')
        if re.fullmatch(r'[0-9]{1,18}', lead_id):
            match |= Q(pk=int(lead_id))
        leads = leads.annotate(full_phone=Concat('country_code', 'phone')).filter(match)

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


class LeadPagination(PageNumberPagination):
    page_size_query_param = 'page_size'
    max_page_size = 100


class LeadViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    # No delete: a lead keeps its history, and Lost is how a lead is closed.
    serializer_class = LeadSerializer
    pagination_class = LeadPagination
    lookup_value_regex = '[0-9]+'

    def get_queryset(self):
        leads = Lead.objects.select_related('plan', 'assigned_to', 'created_by')
        if self.action in ('list', 'export'):
            return filter_leads(leads, self.request.query_params)
        return leads

    def get_permissions(self):
        if self.action == 'export':
            # The whole customer list with phone numbers leaves the system: admins only.
            return [IsAdminRole()]
        if self.action in ('change_status', 'convert'):
            return [CanChangeLeads()]
        if self.action == 'assignees':
            return [CanViewLeads()]
        return [ModelPermissions()]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

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
        lead.convert(request.user)
        return Response(self.get_serializer(lead).data)

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
        """The active users a lead can be assigned to: only ids and names."""
        return Response(list(User.objects.filter(is_active=True).order_by('name', 'id').values('id', 'name')))


class SolarPlanListView(ListAPIView):
    """Every plan with its current price, for the lead form and filters. Admins manage prices."""

    queryset = SolarPlan.objects.all()
    serializer_class = SolarPlanSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None
