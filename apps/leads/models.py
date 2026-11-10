import re

from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import OuterRef, Q, Subquery
from django.db.models.functions import Concat, Lower
from rest_framework.exceptions import APIException

from apps.accounts.models import LegacyAssigneeMixin, Role


class LeadConflict(APIException):
    """A status change or conversion that the lead's current state doesn't allow. The API answers 409."""

    status_code = 409
    default_code = 'conflict'


class LeadStatus(models.TextChoices):
    """The Lead Pipeline's system stages: the keys the CRM's rules name. Admins can add their own stages between New and
    Won (PipelineStage); a lead's status is any Lead stage's key."""

    NEW = 'NEW', 'New'
    INITIAL_CONTACT = 'INITIAL_CONTACT', 'Initial Contact'
    HOT = 'HOT', 'Hot'
    SUPERHOT = 'SUPERHOT', 'Superhot'
    WON = 'WON', 'Won'
    LOST = 'LOST', 'Lost'


# The final outcomes, after every open stage. Every other Lead stage, an admin's included, is open.
OUTCOMES = [LeadStatus.WON, LeadStatus.LOST]


class WorkType(models.TextChoices):
    """How the customer pays for the installation, on a lead and on its Work. Optional: empty means not known yet. Not a
    pipeline stage."""

    LOAN = 'LOAN', 'Loan Work'
    CASH = 'CASH', 'Cash Work'


class Pipeline(models.TextChoices):
    LEAD = 'LEAD', 'Lead Pipeline'
    WORK = 'WORK', 'Work Pipeline'


# Per pipeline, the system stage that is always first (where new records start) and the stages that are always last.
# An admin's stages sit between them.
FIRST_STAGE = {Pipeline.LEAD: LeadStatus.NEW, Pipeline.WORK: 'LOAN_DOCUMENTS'}
LAST_STAGES = {Pipeline.LEAD: OUTCOMES, Pipeline.WORK: ['COMPLETED']}


class PipelineStage(models.Model):
    """A stage of the Lead or the Work Pipeline, in the order the boards show them. A lead stores its stage's key as its
    status, a Work as its stage, as they stored the fixed choices before; keys are unique across both pipelines, so a key
    always says which pipeline it belongs to.

    System stages (is_system) are the ones the CRM's rules name (LeadStatus, works.WorkStage): they are never renamed,
    moved or deleted. Admins add their own (Settings → Pipeline Stages), rename and reorder them, and delete one only
    once its records have moved to another stage of the same pipeline (PipelineStageViewSet)."""

    pipeline = models.CharField(max_length=4, choices=Pipeline.choices)
    # What leads and Works store. Never changes, so renaming a stage touches no record. As wide as Work.stage: system keys
    # run to 21 characters (SUBSIDY_DOCUMENTATION), admins' to 15 (CUSTOM_ + 8).
    key = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=50)
    # The board's order within the pipeline: 0, 1, 2… (renumber() keeps it so).
    order = models.PositiveIntegerField(default=0)
    is_system = models.BooleanField(default=False)
    # Every stage is active today; an inactive one would be left off the boards and refused as a destination.
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['pipeline', 'order', 'pk']
        constraints = [
            models.CheckConstraint(condition=models.Q(pipeline__in=Pipeline.values), name='leads_stage_pipeline_valid'),
            # "Site visit" and "site visit" are the same name: once per pipeline.
            models.UniqueConstraint(Lower('name'), 'pipeline', name='leads_stage_name_once_per_pipeline'),
        ]

    def __str__(self):
        return f'{self.name} ({self.get_pipeline_display()})'

    @classmethod
    def renumber(cls, pipeline, ordered=None):
        """Stores `ordered` (the pipeline's stages, in order; by default as they are) as 0, 1, 2…, with the first stage
        first and the last stages last whatever order was given."""
        stages = list(cls.objects.filter(pipeline=pipeline)) if ordered is None else ordered
        first, last = FIRST_STAGE[pipeline], LAST_STAGES[pipeline]
        rank = lambda stage: 0 if stage.key == first else 2 if stage.key in last else 1  # noqa: E731
        stages = sorted(stages, key=rank)  # stable: the given order within each group
        for order, stage in enumerate(stages):
            if stage.order != order:
                stage.order = order
                stage.save(update_fields=['order', 'updated_at'])
        return stages


def stage_keys(pipeline, open_only=False):
    """The pipeline's active stages' keys, in order; with open_only, without the last stages (Won and Lost, Completed)."""
    stages = PipelineStage.objects.filter(pipeline=pipeline, is_active=True)
    if open_only:
        stages = stages.exclude(key__in=LAST_STAGES[pipeline])
    return list(stages.values_list('key', flat=True))


def stage_choices(pipeline):
    """The pipeline's active stages as (key, name) in order: what the fixed choices were."""
    return list(PipelineStage.objects.filter(pipeline=pipeline, is_active=True).values_list('key', 'name'))


def lock_stage(pipeline, key):
    """Locks the stage a record is moving to, until the request commits, so an admin can't delete it while the record
    moves in: a deletion waits, then moves this record too. Locked before the record itself, as a deletion locks its
    stages before their records, so the two never deadlock. Refuses a stage that is gone or belongs to the other
    pipeline."""
    stage = PipelineStage.objects.select_for_update().filter(pipeline=pipeline, key=key, is_active=True).first()
    if stage is None:
        raise LeadConflict('This stage no longer exists. Reload the page and choose another.')
    return stage


def stage_rank(field):
    """The board position of the stage whose key is in `field`, for sorting records by stage. Keys are unique across
    both pipelines, so a key alone finds its stage."""
    return Subquery(PipelineStage.objects.filter(key=OuterRef(field)).values('order')[:1])


def stage_name_of(field):
    """The current name of the stage whose key is in `field`, for annotating a query as `stage_label`
    (records.with_latest_activity), so a page of records doesn't read each one's stage separately."""
    return Subquery(PipelineStage.objects.filter(key=OuterRef(field)).values('name')[:1])


def stage_name(key, annotated=None):
    """A stage's name: the annotated one (stage_name_of) when the query has it, otherwise read now. The key itself for a
    stage that no longer exists."""
    if annotated is None:
        annotated = PipelineStage.objects.filter(key=key).values_list('name', flat=True).first()
    return annotated or key


class LeadSource(models.TextChoices):
    WALK_IN = 'WALK_IN', 'Walk-in'
    PHONE_CALL = 'PHONE_CALL', 'Phone call'
    WHATSAPP = 'WHATSAPP', 'WhatsApp'
    REFERRAL = 'REFERRAL', 'Referral'
    WEBSITE = 'WEBSITE', 'Website'
    SOCIAL_MEDIA = 'SOCIAL_MEDIA', 'Social media'
    OTHER = 'OTHER', 'Other'


class SolarPlan(models.Model):
    """A system size the business sells. `amount` is its current price: the default for new leads only."""

    name = models.CharField(max_length=50, unique=True)
    capacity = models.DecimalField(max_digits=5, decimal_places=2, unique=True, help_text='System size in kW.')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['capacity']
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gte=0), name='leads_solarplan_amount_non_negative'),
        ]

    def __str__(self):
        return self.name


class LeadQuerySet(models.QuerySet):
    def visible_to(self, user):
        """The leads a user works with: every lead for an admin, only the leads assigned to them for staff.
        What they may do with those leads still comes from their role (the Leads module's permissions)."""
        # One row per lead: the join matches at most one of a lead's assignees.
        return self if user.role_id == Role.ADMIN else self.filter(assignees=user)

    def search(self, text, details=True):
        """The leads a search box entry finds: by name, email, area, district, PIN code or dealer name; by phone number,
        with or without its country code, ignoring spaces and a leading 0; or by ID, so "1024" and "#1024" find lead
        1024. Without `details`, only by name, phone or ID."""
        text = text.strip()
        if not text:
            return self
        match = Q(name__icontains=text)
        if details:
            match |= (
                Q(email__icontains=text) | Q(area__icontains=text) | Q(district__icontains=text)
                | Q(pin_code__icontains=text) | Q(dealer_name__icontains=text)
            )
        if re.fullmatch(r'[0-9\s()+-]+', text):
            digits = re.sub(r'[^0-9]', '', text).lstrip('0')
            if digits:
                match |= Q(full_phone__contains=digits)
        lead_id = text.removeprefix('#')
        if re.fullmatch(r'[0-9]{1,18}', lead_id):
            match |= Q(pk=int(lead_id))
        return self.annotate(full_phone=Concat('country_code', 'phone')).filter(match)


class Lead(LegacyAssigneeMixin, models.Model):
    """A sales enquiry. Only the customer's name and phone number are required; the rest is filled in as it is known."""

    name = models.CharField(max_length=150)
    # Kept apart from the number, so WhatsApp and call links never guess the country: "91" + "9876543210".
    country_code = models.CharField(
        max_length=3,
        default='91',
        validators=[RegexValidator(r'^[1-9][0-9]{0,2}$', 'Enter a country code of 1 to 3 digits.')],
    )
    phone = models.CharField(
        max_length=14,
        validators=[RegexValidator(r'^[0-9]{6,14}$', 'Enter the phone number as 6 to 14 digits.')],
    )
    email = models.EmailField(blank=True)
    state = models.CharField(max_length=100, blank=True)
    district = models.CharField(max_length=100, blank=True)
    area = models.CharField(max_length=150, blank=True)
    pin_code = models.CharField(
        max_length=6,
        blank=True,
        validators=[RegexValidator(r'^[1-9][0-9]{5}$', 'Enter a 6-digit PIN code.')],
    )
    # The dealer the enquiry came through, if any.
    dealer_name = models.CharField(max_length=150, blank=True)
    # Optional until the customer chooses a system; a lead needs one (and its amount) to be converted into a Work.
    plan = models.ForeignKey(SolarPlan, on_delete=models.PROTECT, null=True, blank=True, related_name='leads')
    # The lead's own price. It starts as the plan's current price when a plan is chosen and never follows later price
    # changes. Empty while there is no plan and none was entered.
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    # Loan or cash, once known; carried into the Work when the lead is converted. Empty: not chosen.
    work_type = models.CharField(max_length=4, choices=WorkType.choices, blank=True)
    # The key of a Lead stage (PipelineStage): a system one (LeadStatus) or an admin's. Changes only through move_to().
    status = models.CharField(max_length=20, default=LeadStatus.NEW)
    source = models.CharField(max_length=20, choices=LeadSource.choices, blank=True)
    # Any number of staff work the lead (see LeadAssignee).
    assignees = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through='LeadAssignee', blank=True, related_name='assigned_leads',
    )
    next_follow_up = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    # Shared by the whole team: pinned leads are listed first for everyone.
    is_pinned = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_leads')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = LeadQuerySet.as_manager()

    class Meta:
        # No check on the status: an admin's stages are valid too. The API accepts only a Lead stage's key, and a stage is
        # deleted only once its leads have moved (PipelineStageViewSet).
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gte=0), name='leads_lead_amount_non_negative'),
            models.CheckConstraint(
                condition=models.Q(work_type__in=['', *WorkType.values]), name='leads_lead_work_type_valid',
            ),
        ]
        indexes = [
            models.Index(fields=['status'], name='leads_lead_status_idx'),
            models.Index(fields=['created_at'], name='leads_lead_created_at_idx'),
            models.Index(fields=['next_follow_up'], name='leads_lead_follow_up_idx'),
        ]

    def __str__(self):
        return self.name

    def get_status_display(self):
        """The status's name, as an admin may have renamed it (PipelineStage)."""
        return stage_name(self.status, getattr(self, 'stage_label', None))

    def allowed_transitions(self, open_stages=None):
        """From any open stage: forward to any later open stage (in the board's order, an admin's stages included), to
        Won, or to Lost. Won and Lost are final: a lead is never moved back or reopened. `open_stages` is
        stage_keys(LEAD, open_only=True), read once by callers that check many leads."""
        if self.status in OUTCOMES:
            return []
        if open_stages is None:
            open_stages = stage_keys(Pipeline.LEAD, open_only=True)
        # A status that isn't an open stage (never expected) may still move to any of them, rather than be stuck.
        later = open_stages[open_stages.index(self.status) + 1:] if self.status in open_stages else open_stages
        return [*later, *OUTCOMES]

    def move_to(self, new_status):
        """Moves the lead through the pipeline. Reaching Won doesn't create the Work: convert() does that next."""
        with transaction.atomic():
            # The stage first, then the lead (see lock_stage).
            lock_stage(Pipeline.LEAD, new_status)
            # Locked, so two people changing the same lead can't both act on its old status.
            lead = Lead.objects.select_for_update().get(pk=self.pk)
            if new_status == lead.status:
                # Reachable through the bulk status action (a lead already in the chosen status).
                raise LeadConflict(f'The lead is already {lead.get_status_display()}.')
            if new_status not in lead.allowed_transitions():
                raise LeadConflict(f"A {lead.get_status_display()} lead can't move to {stage_name(new_status)}.")
            lead.status = new_status
            lead.save(update_fields=['status', 'updated_at'])
        self.status, self.updated_at = lead.status, lead.updated_at

    def convert(self, user):
        """Creates the Work for a Won lead. Only Won leads convert, each only once, and only with a plan and an amount (the
        Work's confirmed price); converting never changes the lead's status: whoever works the lead moves it to Won
        first, and it stays Won."""
        # The Works module copies the customer, plan and lead.amount into the Work. Imported here because the Works
        # module depends on leads, not the other way round.
        from apps.works.models import Work

        with transaction.atomic():
            # Locked, so two conversions of the same lead run one after the other and can't both create a Work.
            lead = Lead.objects.select_for_update().get(pk=self.pk)
            if lead.status != LeadStatus.WON:
                raise LeadConflict('Only Won leads can be converted.')
            if Work.objects.filter(lead=lead).exists():
                raise LeadConflict('This lead has already been converted.')
            if lead.plan_id is None or lead.amount is None:
                raise LeadConflict("Choose the lead's solar plan and amount before converting it to a Work.")
            self.work = Work.create_for_lead(lead, user)
        # As the locked row has it (Won), should this copy have been read before a concurrent move to Won committed.
        self.status, self.updated_at = lead.status, lead.updated_at


class LeadAssignee(models.Model):
    """One of the staff a lead is assigned to. PROTECT, as the one assignee was: someone with assigned leads is
    deactivated, never deleted (see the Users API)."""

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='+')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['lead', 'user'], name='leads_assignee_once_per_lead')]
