from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models, transaction
from rest_framework.exceptions import APIException


class LeadConflict(APIException):
    """A status change or conversion that the lead's current state doesn't allow. The API answers 409."""

    status_code = 409
    default_code = 'conflict'


class LeadStatus(models.TextChoices):
    NEW = 'NEW', 'New'
    INITIAL_CONTACT = 'INITIAL_CONTACT', 'Initial Contact'
    HOT = 'HOT', 'Hot'
    SUPERHOT = 'SUPERHOT', 'Superhot'
    WON = 'WON', 'Won'
    LOST = 'LOST', 'Lost'


# The open stages in pipeline order. Won and Lost are final outcomes.
PIPELINE = [LeadStatus.NEW, LeadStatus.INITIAL_CONTACT, LeadStatus.HOT, LeadStatus.SUPERHOT]


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


class Lead(models.Model):
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
    district = models.CharField(max_length=100)
    area = models.CharField(max_length=150, blank=True)
    pin_code = models.CharField(
        max_length=6,
        blank=True,
        validators=[RegexValidator(r'^[1-9][0-9]{5}$', 'Enter a 6-digit PIN code.')],
    )
    plan = models.ForeignKey(SolarPlan, on_delete=models.PROTECT, related_name='leads')
    # The lead's own price. It starts as the plan's current price and never follows later price changes.
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    status = models.CharField(max_length=20, choices=LeadStatus.choices, default=LeadStatus.NEW)
    source = models.CharField(max_length=20, choices=LeadSource.choices, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='assigned_leads',
    )
    next_follow_up = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    # Shared by the whole team: pinned leads are listed first for everyone.
    is_pinned = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_leads')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(status__in=LeadStatus.values), name='leads_lead_status_valid'),
            models.CheckConstraint(condition=models.Q(amount__gte=0), name='leads_lead_amount_non_negative'),
        ]
        indexes = [
            models.Index(fields=['status'], name='leads_lead_status_idx'),
            models.Index(fields=['created_at'], name='leads_lead_created_at_idx'),
            models.Index(fields=['next_follow_up'], name='leads_lead_follow_up_idx'),
        ]

    def __str__(self):
        return self.name

    def allowed_transitions(self):
        """Forward to any later stage, to Lost from any open stage, and to Won (by conversion) from Superhot.
        Won and Lost are final: a lead is never moved back or reopened."""
        if self.status not in PIPELINE:
            return []
        later = PIPELINE[PIPELINE.index(self.status) + 1:]
        won = [LeadStatus.WON] if self.status == LeadStatus.SUPERHOT else []
        return [*later, *won, LeadStatus.LOST]

    def move_to(self, new_status):
        """Moves the lead through the pipeline. Won is reached only by convert(), which also creates the Work."""
        with transaction.atomic():
            # Locked, so two people changing the same lead can't both act on its old status.
            lead = Lead.objects.select_for_update().get(pk=self.pk)
            if new_status == LeadStatus.WON:
                raise LeadConflict('Use Convert to mark a lead Won; converting also creates its Work.')
            if new_status not in lead.allowed_transitions():
                label = dict(LeadStatus.choices).get(new_status, new_status)
                raise LeadConflict(f"A {lead.get_status_display()} lead can't move to {label}.")
            lead.status = new_status
            lead.save(update_fields=['status', 'updated_at'])
        self.status, self.updated_at = lead.status, lead.updated_at

    def convert(self, user):
        """Marks the lead Won and creates its Work in one transaction, so no lead is ever Won without its Work."""
        with transaction.atomic():
            lead = Lead.objects.select_for_update().get(pk=self.pk)
            if lead.status == LeadStatus.WON:
                raise LeadConflict('This lead has already been converted.')
            if LeadStatus.WON not in lead.allowed_transitions():
                raise LeadConflict('Only Superhot leads can be converted.')
            # The Works module copies the customer, plan and lead.amount into the Work. Imported here because the
            # Works module depends on leads, not the other way round.
            from apps.works.models import Work

            work = Work.create_for_lead(lead, user)
            lead.status = LeadStatus.WON
            lead.save(update_fields=['status', 'updated_at'])
        self.status, self.updated_at, self.work = lead.status, lead.updated_at, work
