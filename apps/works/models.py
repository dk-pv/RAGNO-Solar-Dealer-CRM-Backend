from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.db.models.functions import Coalesce

from apps.leads.models import WorkType, stage_name

from . import storage
from .documents import ADDITIONAL, ADDITIONAL_CATEGORIES, FILE_DOCUMENTS

# Work.effective_amount in a query, for sums and sorting: the offer price where an admin applied one, otherwise the
# standard amount.
EFFECTIVE_AMOUNT = Coalesce('offer_amount', 'amount')


class WorkStage(models.TextChoices):
    """The Work Pipeline's system stages, in order. Admins can add their own between Loan Work / Documents and Completed
    (leads.PipelineStage); a Work's stage is any Work stage's key. A Work can move to any stage, in either direction."""

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
    # The standard price: the lead's amount at conversion. It never follows later plan price changes, and an offer never
    # replaces it.
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    # A special price an admin gave this customer alone (the offer action), or null. What the customer pays is
    # effective_amount; `amount` stays for reference.
    offer_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Why the offer was given, as the admin noted it (optional).
    offer_note = models.TextField(blank=True)
    # Loan or cash: the lead's at conversion, empty if it had none; it can be set or changed on the Work afterwards.
    work_type = models.CharField(max_length=4, choices=WorkType.choices, blank=True)
    # The key of a Work stage (leads.PipelineStage): a system one (WorkStage) or an admin's.
    stage = models.CharField(max_length=30, default=WorkStage.LOAN_DOCUMENTS)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='assigned_works',
    )
    due_date = models.DateField(null=True, blank=True)
    # Shared by the whole team, as on leads: pinned Works are listed first for everyone.
    is_pinned = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_works')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # No check on the stage: an admin's stages are valid too. The API accepts only a Work stage's key, and a stage is
        # deleted only once its Works have moved (leads.PipelineStageViewSet).
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gte=0), name='works_work_amount_non_negative'),
            models.CheckConstraint(
                condition=models.Q(work_type__in=['', *WorkType.values]), name='works_work_work_type_valid',
            ),
            # An offer is a price above zero and at most the standard amount: a special price, never a surcharge.
            models.CheckConstraint(
                condition=models.Q(offer_amount__isnull=True)
                | models.Q(offer_amount__gt=0, offer_amount__lte=models.F('amount')),
                name='works_work_offer_within_amount',
            ),
        ]
        indexes = [
            models.Index(fields=['stage'], name='works_work_stage_idx'),
            models.Index(fields=['due_date'], name='works_work_due_date_idx'),
        ]

    def __str__(self):
        return f'Work #{self.pk} · {self.customer_name}'

    @property
    def effective_amount(self):
        """What the customer pays: the offer price if an admin applied one, otherwise the standard amount."""
        return self.amount if self.offer_amount is None else self.offer_amount

    def get_stage_display(self):
        """The stage's name, as an admin may have renamed it (leads.PipelineStage)."""
        return stage_name(self.stage, getattr(self, 'stage_label', None))

    def delete(self, *args, files=None, **kwargs):
        """With its documents and, once that commits, their files (see WorkDocumentQuerySet.delete for `files`). They
        PROTECT the Work, so they never go without them."""
        with transaction.atomic():
            # Locked first, as an upload locks it, so a deletion and an upload of this Work's documents can't deadlock.
            Work.objects.select_for_update().get(pk=self.pk)
            self.documents.all().delete(files=files)
            return super().delete(*args, **kwargs)

    @classmethod
    def create_for_lead(cls, lead, user):
        """The Work for a lead being converted: its customer, plan, amount, work type and assignee as they are now."""
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
            work_type=lead.work_type,
            assigned_to=lead.assigned_to,
            created_by=user,
        )


class WorkDocumentQuerySet(models.QuerySet):
    def delete(self, files=None):
        """Deletes the records, then their files in Cloudinary once that has committed: never the files first, so no
        record is left pointing at a file that is gone. Given a list as `files`, the files are added to it instead, for
        the caller to delete with others in one go after commit; still only if this deletion commits (a savepoint that
        rolls back takes them back out)."""
        found = list(self.values_list('cloudinary_resource_type', 'cloudinary_public_id'))
        deleted = super().delete()
        if found and files is not None:
            transaction.on_commit(lambda: files.extend(found))
        elif found:
            # ponytail: the files are deleted in the request, after commit, 100 per Cloudinary call; move this to a
            # background job if resets or bulk deletes ever carry thousands of files.
            transaction.on_commit(lambda: storage.delete(found), robust=True)
        return deleted

    # As Django's own QuerySet.delete: never on the manager, so WorkDocument.objects.delete() can't empty the table.
    delete.alters_data = True
    delete.queryset_only = True


class WorkDocument(models.Model):
    """A file uploaded for a Work, kept privately in Cloudinary (storage.py): either one of its required documents
    (documents.py), one per document (uploading it again replaces it), or an additional document (document_key
    ADDITIONAL), any number of them, each with its own title. Additional documents never count towards the required
    ones (documents.summarize reads only the required keys).

    Records are deleted through the queryset (Work.delete too), which removes their files as well. They PROTECT their
    Work, so a deletion that would cascade past that, leaving the files behind, fails instead."""

    work = models.ForeignKey(Work, on_delete=models.PROTECT, related_name='documents')
    document_key = models.CharField(max_length=40, choices=[*FILE_DOCUMENTS, (ADDITIONAL, 'Additional document')])
    # An additional document's own details, as the uploader gave them; empty for a required document.
    title = models.CharField(max_length=150, blank=True)
    category = models.CharField(max_length=20, choices=ADDITIONAL_CATEGORIES, blank=True)
    description = models.TextField(blank=True)
    cloudinary_public_id = models.CharField(max_length=255, unique=True)
    cloudinary_resource_type = models.CharField(max_length=10)
    # As uploaded (Django cleans it): what a download is saved as.
    original_filename = models.CharField(max_length=255)
    # Read from the file's contents (documents.check_file), not taken from the browser.
    content_type = models.CharField(max_length=100)
    file_size = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='uploaded_documents',
    )
    # The latest upload: replacing the file moves it, while created_at keeps the first.
    uploaded_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkDocumentQuerySet.as_manager()

    class Meta:
        constraints = [
            # One file per required document; any number of additional ones.
            models.UniqueConstraint(
                fields=['work', 'document_key'], condition=~Q(document_key=ADDITIONAL), name='works_document_one_per_key',
            ),
            models.CheckConstraint(
                condition=models.Q(document_key__in=[*(key for key, _ in FILE_DOCUMENTS), ADDITIONAL]),
                name='works_document_key_valid',
            ),
        ]

    def __str__(self):
        return f'{self.title or self.get_document_key_display()} · Work #{self.work_id}'

    def delete(self, *args, **kwargs):
        # Through the queryset, so the file goes too.
        return WorkDocument.objects.filter(pk=self.pk).delete()

    @classmethod
    def save_upload(cls, work, key, user, *, public_id, resource_type, filename, content_type, size):
        """Makes a newly stored file the Work's `key` document. Returns the file it replaced, if any, as (resource type,
        public id), for the caller to delete once this has committed."""
        # Locked, so two uploads to the same Work run one after the other and can't both create the same document.
        Work.objects.select_for_update().get(pk=work.pk)
        document = cls.objects.filter(work=work, document_key=key).first() or cls(work=work, document_key=key)
        replaced = (document.cloudinary_resource_type, document.cloudinary_public_id) if document.pk else None
        document.cloudinary_public_id, document.cloudinary_resource_type = public_id, resource_type
        document.original_filename, document.content_type, document.file_size = filename, content_type, size
        document.uploaded_by = user
        document.save()
        return replaced

    @classmethod
    def save_additional(cls, work, user, *, title, category, description, public_id, resource_type, filename,
                        content_type, size):
        """Records a newly stored file as one more additional document of the Work."""
        # Locked, as save_upload locks it, so a Work being deleted meanwhile can't be left with a new document.
        Work.objects.select_for_update().get(pk=work.pk)
        return cls.objects.create(
            work=work, document_key=ADDITIONAL, title=title, category=category, description=description,
            cloudinary_public_id=public_id, cloudinary_resource_type=resource_type, original_filename=filename,
            content_type=content_type, file_size=size, uploaded_by=user,
        )
