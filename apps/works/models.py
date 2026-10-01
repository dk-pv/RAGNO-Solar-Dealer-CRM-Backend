from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class WorkStage(models.TextChoices):
    """The Work Pipeline, in order. A Work can move to any stage, in either direction."""

    LOAN_DOCUMENTS = 'LOAN_DOCUMENTS', 'Loan Work / Documents'
    FEASIBILITY = 'FEASIBILITY', 'Feasibility'
    STRUCTURE = 'STRUCTURE', 'Structure Work'
    ELECTRICAL = 'ELECTRICAL', 'Electrical Work'
    KSEB_DOCUMENTATION = 'KSEB_DOCUMENTATION', 'KSEB Documentation'
    SUBSIDY_DOCUMENTATION = 'SUBSIDY_DOCUMENTATION', 'Subsidy Documentation'
    COMPLETED = 'COMPLETED', 'Completed'


class Work(models.Model):
    """The installation job for a converted lead. Created only by converting the lead (Lead.convert)."""

    lead = models.OneToOneField('leads.Lead', on_delete=models.PROTECT, related_name='work')
    # The customer as they were at conversion, so later edits to the lead never rewrite the job's record.
    customer_name = models.CharField(max_length=150)
    country_code = models.CharField(max_length=3)
    phone = models.CharField(max_length=14)
    email = models.EmailField(blank=True)
    state = models.CharField(max_length=100, blank=True)
    district = models.CharField(max_length=100)
    area = models.CharField(max_length=150, blank=True)
    pin_code = models.CharField(max_length=6, blank=True)
    plan = models.ForeignKey('leads.SolarPlan', on_delete=models.PROTECT, related_name='works')
    # The confirmed price: the lead's amount at conversion. It never follows later plan price changes.
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    stage = models.CharField(max_length=30, choices=WorkStage.choices, default=WorkStage.LOAN_DOCUMENTS)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='assigned_works',
    )
    due_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_works')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(stage__in=WorkStage.values), name='works_work_stage_valid'),
            models.CheckConstraint(condition=models.Q(amount__gte=0), name='works_work_amount_non_negative'),
        ]
        indexes = [
            models.Index(fields=['stage'], name='works_work_stage_idx'),
            models.Index(fields=['due_date'], name='works_work_due_date_idx'),
        ]

    def __str__(self):
        return f'Work #{self.pk} · {self.customer_name}'

    @classmethod
    def create_for_lead(cls, lead, user):
        """The Work for a lead being converted: its customer, plan, amount and assignee as they are now."""
        return cls.objects.create(
            lead=lead,
            customer_name=lead.name,
            country_code=lead.country_code,
            phone=lead.phone,
            email=lead.email,
            state=lead.state,
            district=lead.district,
            area=lead.area,
            pin_code=lead.pin_code,
            plan=lead.plan,
            amount=lead.amount,
            assigned_to=lead.assigned_to,
            created_by=user,
        )
