import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Case, Count, F, IntegerField, Min, Q, Sum, Value, When
from django.db.models.functions import Concat
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.activities.models import ActivityStatus

from .models import Work, WorkStage
from .serializers import WorkQuerySerializer, WorkSerializer

User = get_user_model()

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
    # No create or delete: converting a lead creates its Work, and a Work keeps the job's history.
    # No PUT: only the stage, assignee, due date and pin change.
    http_method_names = ['get', 'patch', 'head', 'options']
    serializer_class = WorkSerializer
    permission_classes = [CanUseWorks]
    pagination_class = WorkPagination
    lookup_value_regex = '[0-9]+'

    def get_queryset(self):
        works = Work.objects.select_related('plan', 'assigned_to')
        if self.action in ('list', 'summary'):
            works = filter_works(works, self.request.query_params)
        # Not for the summary: joining the activities would count each Work's amount once per activity.
        return works if self.action == 'summary' else works.annotate(**ACTIVITY_STATS)

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
