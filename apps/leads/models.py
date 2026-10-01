from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models, transaction
from rest_framework.exceptions import APIException

from apps.accounts.models import Role


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


class LeadQuerySet(models.QuerySet):
    def visible_to(self, user):
        """The leads a user works with: every lead for an admin, only the leads assigned to them for staff.
        What they may do with those leads still comes from their role (the Leads module's permissions)."""
        return self if user.role_id == Role.ADMIN else self.filter(assigned_to=user)


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

    objects = LeadQuerySet.as_manager()

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
        """From any open stage: forward to any later stage, to Won, or to Lost.
        Won and Lost are final: a lead is never moved back or reopened."""
        if self.status not in PIPELINE:
            return []
        return [*PIPELINE[PIPELINE.index(self.status) + 1:], LeadStatus.WON, LeadStatus.LOST]

    def move_to(self, new_status):
        """Moves the lead through the pipeline. Reaching Won doesn't create the Work: convert() does that next."""
        with transaction.atomic():
            # Locked, so two people changing the same lead can't both act on its old status.
            lead = Lead.objects.select_for_update().get(pk=self.pk)
            if new_status not in lead.allowed_transitions():
                label = dict(LeadStatus.choices).get(new_status, new_status)
                raise LeadConflict(f"A {lead.get_status_display()} lead can't move to {label}.")
            lead.status = new_status
            lead.save(update_fields=['status', 'updated_at'])
        self.status, self.updated_at = lead.status, lead.updated_at

    def convert(self, user):
        """Creates the Work for a Won lead. Only Won leads convert, and converting never changes the lead's status:
        whoever works the lead moves it to Won first."""
        with transaction.atomic():
            # Locked, so two conversions of the same lead run one after the other and can't both create a Work.
            lead = Lead.objects.select_for_update().get(pk=self.pk)
            if lead.status != LeadStatus.WON:
                raise LeadConflict(f'Only Won leads can be converted. This lead is {lead.get_status_display()}.')
            # Creating the Work belongs to the Works module (HRITHIK), which isn't in this codebase yet. Once it is, the
            # Work links to its lead with a one-to-one field, so the database itself refuses a second Work for a lead:
            # return the lead's Work if it already has one, otherwise create it here through that module for `user`
            # (copying the customer, plan and lead.amount). Until then conversion is refused and the lead stays Won.
            raise LeadConflict("Converting creates the lead's Work, and the Works module isn't available yet.")
