"""Adds 50 fictional demo leads, and follow-ups on some of them, for development and demos. Safe to run repeatedly.

    python manage.py seed_demo_leads

It runs only with DJANGO_DEBUG=True, so it can't add demo data to a production database. It never deletes or
changes existing leads. Names, emails (example.com) and phone numbers are made up, but a made-up number can still
belong to someone, so don't call or message seeded leads.
"""
from collections import Counter
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role
from apps.activities.models import Activity, ActivityStatus, ActivityType
from apps.leads.models import Lead, LeadConflict, LeadSource, LeadStatus, SolarPlan

User = get_user_model()
S = LeadStatus
SRC = LeadSource

# The business's current prices, used only to create a plan that doesn't exist yet. Existing plans keep their prices.
PLANS = {3: ('3 kW', 150000), 5: ('5 kW', 200000), 8: ('8 kW', 245000), 10: ('10 kW', 289000)}

# Every lead is in Kerala. Target statuses: New 10, Initial Contact 9, Hot 9, Superhot 8, Won 7, Lost 7.
# name, area, district, PIN, plan kW, amount (None: the plan price), target status, source, days since created,
# next follow-up in days (None: none), pinned, note
LEADS = [
    ('Arjun Nair', 'Kakkanad', 'Ernakulam', '682030', 5, None, S.NEW, SRC.WEBSITE, 1, 1, False,
     'Asked for a quote through the website form.'),
    ('Priya Menon', 'Guruvayur', 'Thrissur', '680101', 3, None, S.NEW, SRC.WHATSAPP, 0, 1, False,
     'Sent photos of her electricity bills on WhatsApp.'),
    ('Rahul Kumar', 'Kazhakoottam', 'Thiruvananthapuram', '695582', 8, None, S.NEW, SRC.PHONE_CALL, 2, 2, False,
     'Runs a small bakery and wants to cut daytime power costs.'),
    ('Anjali Thomas', 'Pala', 'Kottayam', '686575', 5, None, S.NEW, SRC.REFERRAL, 3, 2, False,
     'Referred by a customer from the same parish.'),
    ('Vivek Raj', 'Feroke', 'Kozhikode', '673631', 10, None, S.NEW, SRC.WEBSITE, 1, 3, False,
     'Two-storey house with a large flat roof.'),
    ('Meera Krishnan', 'Cherthala', 'Alappuzha', '688524', 3, None, S.NEW, SRC.SOCIAL_MEDIA, 4, None, False,
     'Asked about the rooftop subsidy on Instagram.'),
    ('Joseph Varghese', 'Thodupuzha', 'Idukki', '685584', 5, None, S.NEW, SRC.WALK_IN, 5, 1, False,
     'Visited the office with his last three bills.'),
    ('Fathima Rasheed', 'Tirur', 'Malappuram', '676101', 8, None, S.NEW, SRC.PHONE_CALL, 2, 3, False,
     'Wants to know the loan options.'),
    ('Sreejith Pillai', 'Karunagappally', 'Kollam', '690518', 3, None, S.NEW, SRC.WEBSITE, 6, 2, False,
     'Enquiry for a small rooftop system.'),
    ('Divya Mohan', 'Kalpetta', 'Wayanad', '673121', 5, None, S.NEW, SRC.OTHER, 3, None, False,
     'Met at the Kalpetta trade fair stall.'),

    ('Abdul Kareem', 'Manjeri', 'Malappuram', '676121', 5, None, S.INITIAL_CONTACT, SRC.PHONE_CALL, 8, 2, False,
     'Called back; the monthly bill is about ₹3,800.'),
    ('Lakshmi Warrier', 'Irinjalakuda', 'Thrissur', '680121', 3, None, S.INITIAL_CONTACT, SRC.REFERRAL, 10, -1, False,
     'Wants the plan brochure in Malayalam.'),
    ('Thomas Chacko', 'Changanassery', 'Kottayam', '686101', 8, None, S.INITIAL_CONTACT, SRC.WALK_IN, 12, 4, False,
     'Asked for a site survey next week.'),
    ('Neethu Babu', 'Thiruvalla', 'Pathanamthitta', '689101', 5, None, S.INITIAL_CONTACT, SRC.WEBSITE, 6, 1, False,
     'Shared roof photos; checking shade from the coconut trees.'),
    ('Suresh Kumar', 'Ottapalam', 'Palakkad', '679101', 10, None, S.INITIAL_CONTACT, SRC.PHONE_CALL, 15, 3, False,
     'Runs a rice mill and is interested in a larger system.'),
    ('Aswathy Nair', 'Attingal', 'Thiruvananthapuram', '695101', 3, None, S.INITIAL_CONTACT, SRC.SOCIAL_MEDIA, 9, 5,
     False, 'Asked about the monthly EMI.'),
    ('Shibu Mathew', 'Kattappana', 'Idukki', '685508', 5, None, S.INITIAL_CONTACT, SRC.WHATSAPP, 14, 2, False,
     'Brochure sent; will discuss with the family.'),
    ('Haritha Das', 'Thalassery', 'Kannur', '670101', 8, None, S.INITIAL_CONTACT, SRC.REFERRAL, 18, -2, False,
     'Referred by her brother, an existing customer.'),
    ('Muhammed Shafi', 'Kanhangad', 'Kasaragod', '671315', 3, None, S.INITIAL_CONTACT, SRC.PHONE_CALL, 11, 6, False,
     'First call done; prefers calls in the evening.'),

    ('Reshma Joseph', 'Aluva', 'Ernakulam', '683101', 5, None, S.HOT, SRC.REFERRAL, 20, 1, True,
     'Site survey done; the roof fits 12 panels.'),
    ('Anoop Chandran', 'Kunnamkulam', 'Thrissur', '680503', 8, 240000, S.HOT, SRC.WEBSITE, 25, 2, False,
     'Comparing quotes; offered a small discount.'),
    ('Sruthi Menon', 'Edappally', 'Ernakulam', '682024', 10, None, S.HOT, SRC.PHONE_CALL, 16, 3, False,
     'Wants the battery backup options explained.'),
    ('Biju Kurian', 'Ettumanoor', 'Kottayam', '686631', 5, None, S.HOT, SRC.WALK_IN, 30, 1, False,
     'Survey done; waiting for the KSEB load details.'),
    ('Nimisha George', 'Adoor', 'Pathanamthitta', '691523', 3, None, S.HOT, SRC.SOCIAL_MEDIA, 22, 4, False,
     'Subsidy documents checklist sent.'),
    ('Ajay Krishna', 'Chittur', 'Palakkad', '678101', 8, None, S.HOT, SRC.PHONE_CALL, 28, 2, False,
     'Farm house with a pump load.'),
    ('Salma Beevi', 'Perinthalmanna', 'Malappuram', '679322', 5, None, S.HOT, SRC.REFERRAL, 13, 5, False,
     'Her husband will visit the office on Saturday.'),
    ('Jithin Paul', 'Muvattupuzha', 'Ernakulam', '686661', 10, None, S.HOT, SRC.WEBSITE, 35, 0, False,
     'Asked for the final quotation.'),
    ('Parvathy Ramesh', 'Payyanur', 'Kannur', '670307', 3, None, S.HOT, SRC.WHATSAPP, 19, 3, False,
     'Quote shared; checking a loan with her bank.'),

    ('Vineeth Varma', 'Chalakudy', 'Thrissur', '680307', 8, None, S.SUPERHOT, SRC.REFERRAL, 32, 1, True,
     'Ready to confirm once the KSEB feasibility is approved.'),
    ('Ann Mary Jose', 'Kottarakkara', 'Kollam', '691506', 5, 195000, S.SUPERHOT, SRC.WEBSITE, 40, 0, False,
     'Agreed on the discounted price; signing this week.'),
    ('Rajeev Menon', 'Neyyattinkara', 'Thiruvananthapuram', '695121', 10, None, S.SUPERHOT, SRC.PHONE_CALL, 27, 2,
     False, 'Loan sanctioned; wants the installation before the monsoon.'),
    ('Sabitha Rahman', 'Kottakkal', 'Malappuram', '676503', 5, None, S.SUPERHOT, SRC.WALK_IN, 45, 1, False,
     'Documents collected; waiting for the advance.'),
    ('Kiran Das', 'Taliparamba', 'Kannur', '670141', 3, None, S.SUPERHOT, SRC.SOCIAL_MEDIA, 24, 2, False,
     'Confirmed the plan on the phone.'),
    ('Gopika Suresh', 'Haripad', 'Alappuzha', '690514', 8, None, S.SUPERHOT, SRC.REFERRAL, 38, 0, False,
     'Final site visit booked with the engineer.'),
    ('Sanjay Nambiar', 'Vadakara', 'Kozhikode', '673101', 10, 295000, S.SUPERHOT, SRC.PHONE_CALL, 50, 3, False,
     'Needs an elevated structure; the price includes it.'),
    ('Febin Thomas', 'Adimali', 'Idukki', '685561', 5, None, S.SUPERHOT, SRC.WHATSAPP, 29, 1, False,
     "Waiting for the bank's loan disbursement date."),

    ('Deepa Unnikrishnan', 'Perumbavoor', 'Ernakulam', '683542', 5, None, S.WON, SRC.REFERRAL, 60, None, False,
     'Confirmed the 5 kW plan and paid the advance.'),
    ('Rasheed Ali', 'Koyilandy', 'Kozhikode', '673305', 8, 250000, S.WON, SRC.WALK_IN, 75, None, False,
     'Confirmed, with extra panels over the car porch.'),
    ('Maneesha Pillai', 'Kayamkulam', 'Alappuzha', '690502', 3, None, S.WON, SRC.WEBSITE, 90, None, False,
     'Confirmed; wants the installation after Onam.'),
    ('Tony Antony', 'Mananthavady', 'Wayanad', '670645', 10, None, S.WON, SRC.PHONE_CALL, 110, None, False,
     'Confirmed for the homestay roof.'),
    ('Keerthana Raj', 'Mannarkkad', 'Palakkad', '678582', 5, None, S.WON, SRC.SOCIAL_MEDIA, 55, None, False,
     'Confirmed; subsidy papers in progress.'),
    ('Ashik Muhammed', 'Nileshwaram', 'Kasaragod', '671314', 8, None, S.WON, SRC.REFERRAL, 82, None, False,
     'Confirmed after the second site visit.'),
    ('Remya Sasidharan', 'Ranni', 'Pathanamthitta', '689672', 3, 148000, S.WON, SRC.WHATSAPP, 68, None, False,
     'Confirmed at the agreed price.'),

    ('Nikhil Prasad', 'Sulthan Bathery', 'Wayanad', '673592', 5, None, S.LOST, SRC.WEBSITE, 45, None, False,
     'Chose another installer with a lower quote.'),
    ('Jency Philip', 'Ramanattukara', 'Kozhikode', '673633', 3, None, S.LOST, SRC.PHONE_CALL, 70, None, False,
     'Postponed the project to next year.'),
    ('Hari Govind', 'Chavara', 'Kollam', '691583', 10, None, S.LOST, SRC.REFERRAL, 95, None, False,
     'The roof is heavily shaded; not feasible.'),
    ('Nazeema Hussain', 'Kondotty', 'Malappuram', '673638', 5, None, S.LOST, SRC.WALK_IN, 50, None, False,
     "Budget didn't fit; may come back for a smaller plan."),
    ('Arun Sebastian', 'Iritty', 'Kannur', '670703', 8, None, S.LOST, SRC.SOCIAL_MEDIA, 38, None, False,
     "Couldn't be reached after five calls."),
    ('Kavitha Menon', 'Kodungallur', 'Thrissur', '680664', 5, None, S.LOST, SRC.WEBSITE, 62, None, False,
     'Selling the house; no longer interested.'),
    ('Midhun Mohan', 'Uppala', 'Kasaragod', '671322', 3, None, S.LOST, SRC.PHONE_CALL, 28, None, False,
     "Went with a relative's installer."),
]


A = ActivityType
# Follow-ups on demo leads, to do and done: lead, heading, type, notes, due in days from the first run, done, and who
# does it: 'lead' for the lead's assigned staff, 'other' for another staff member (a follow-up's staff is separate).
FOLLOW_UPS = [
    ('Arjun Nair', 'Call about the 5 kW quotation', A.PHONE_CALL, 'Call to walk him through the 5 kW quote.', 1, False, 'lead'),
    ('Priya Menon', 'Send an estimate from her bills', A.FOLLOW_UP, 'Reply on WhatsApp with an estimate from her bills.', 0, False, 'lead'),
    ('Rahul Kumar', 'Check the roof for shade', A.SITE_VISIT, 'Check the bakery roof for shade from the water tank.', 3, False, 'other'),
    ('Abdul Kareem', 'Confirm roof size and load', A.PHONE_CALL, 'Confirm the roof size and the sanctioned load.', -2, False, 'lead'),
    ('Lakshmi Warrier', 'Send the Malayalam brochure', A.NOTE, 'Posted the Malayalam brochure.', -3, True, 'lead'),
    ('Thomas Chacko', 'Confirm site visit', A.SITE_VISIT, 'Site survey with the installation team.', 4, False, 'other'),
    ('Reshma Joseph', 'Follow up on 5 kW proposal', A.MEETING, 'Go through the 5 kW proposal with her family.', 1, False, 'lead'),
    ('Sruthi Menon', 'Send revised quotation', A.FOLLOW_UP, 'Send the revised 10 kW quotation.', -1, False, 'lead'),
    ('Biju Kurian', 'Discuss financing option', A.PHONE_CALL, 'Explained the subsidy paperwork.', -4, True, 'lead'),
    ('Vineeth Varma', 'Proposal discussion', A.MEETING, 'Proposal discussion at the office.', -2, True, 'lead'),
    ('Rajeev Menon', 'Confirm loan approval date', A.FOLLOW_UP, 'Confirm the loan approval date.', 2, False, 'other'),
    ('Deepa Unnikrishnan', 'Collect the advance payment', A.PHONE_CALL, 'Collect the advance payment.', -6, True, 'lead'),
    ('Keerthana Raj', 'Confirm installation requirements', A.SITE_VISIT, 'Final roof measurement before installation.', 5, False, 'lead'),
    ('Nazeema Hussain', 'Check back about a smaller plan', A.NOTE, 'Chose a smaller plan elsewhere; check back in six months.', -10, True, 'lead'),
]


def phone_for(index):
    # Fixed per position, so a re-run finds the leads it already created instead of adding duplicates.
    return f"{'9876'[index % 4]}{(index * 104729023 + 381654729) % 10**9:09d}"


class Command(BaseCommand):
    help = 'Adds 50 fictional demo leads and some follow-ups (development only; safe to run repeatedly).'

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('Demo leads are for development only: run this with DJANGO_DEBUG=True.')

        owner = (
            User.objects.filter(is_active=True, role=Role.ADMIN).order_by('id').first()
            or User.objects.filter(is_active=True).order_by('id').first()
        )
        if owner is None:
            raise CommandError(
                'There is no active user to own the demo leads. Create one first: python manage.py createsuperuser'
            )
        team = list(User.objects.filter(is_active=True, role=Role.STAFF).order_by('name', 'id')) or [owner]
        today = timezone.localdate()
        now = timezone.now()
        created = present = assigned = follow_ups_added = converted = 0
        not_converted = []
        demo = {}  # name -> the demo lead

        with transaction.atomic():
            plans = {
                kw: SolarPlan.objects.get_or_create(
                    capacity=Decimal(kw), defaults={'name': name, 'amount': Decimal(amount)},
                )[0]
                for kw, (name, amount) in PLANS.items()
            }
            for index, row in enumerate(LEADS):
                name, area, district, pin, kw, amount, target, source, days_ago, follow_up, pinned, note = row
                lead = Lead.objects.filter(country_code='91', phone=phone_for(index)).first()
                if lead and lead.name != name:
                    continue  # Not a demo lead, just the same number: leave it alone.
                if lead:
                    present += 1
                else:
                    # Half of the newest enquiries are still waiting to be assigned; the rest go round the team.
                    assignee = None if target == S.NEW and index % 2 else team[assigned % len(team)]
                    assigned += assignee is not None
                    lead = Lead(
                        name=name,
                        country_code='91',
                        phone=phone_for(index),
                        email=f"{'.'.join(name.lower().split())}@example.com" if index % 2 == 0 else '',
                        state='Kerala',
                        district=district,
                        area=area,
                        pin_code=pin,
                        plan=plans[kw],
                        amount=Decimal(amount) if amount else plans[kw].amount,
                        source=source,
                        assigned_to=assignee,
                        next_follow_up=today + timedelta(days=follow_up) if follow_up is not None else None,
                        notes=note,
                        is_pinned=pinned,
                        created_by=owner,
                    )
                    lead.full_clean()
                    lead.save()
                    # Statuses are reached through the pipeline rules, never set directly.
                    if target != S.NEW:
                        lead.move_to(S.SUPERHOT if target == S.WON else target)
                    created_at = now - timedelta(days=days_ago, hours=index % 8, minutes=(index * 7) % 60)
                    Lead.objects.filter(pk=lead.pk).update(
                        created_at=created_at, updated_at=created_at + timedelta(days=min(days_ago, 1 + index % 4)),
                    )
                    created += 1
                demo[name] = lead
                # Won like any status (from Superhot), then converted to create its Work, once: running the command
                # again converts only the Won demo leads still without their Work.
                if target == S.WON and lead.status == S.SUPERHOT:
                    lead.move_to(S.WON)
                if target == S.WON and lead.status == S.WON and not hasattr(lead, 'work'):
                    try:
                        lead.convert(owner)
                        converted += 1
                    except LeadConflict as refusal:
                        not_converted.append((name, str(refusal.detail)))

            # A demo follow-up already there (same lead, same notes) keeps its status; only details it is missing (a
            # heading, its staff, a due date: older demo follow-ups had none) are filled in.
            for name, title, kind, description, due_in, done, doer in FOLLOW_UPS:
                if name not in demo:
                    continue
                lead = demo[name]
                staff = lead.assigned_to or owner
                if doer == 'other' and len(team) > 1:
                    staff = next(person for person in team if person != lead.assigned_to)
                details = {'title': title, 'assigned_to': staff, 'due_date': today + timedelta(days=due_in)}
                follow_up, added = Activity.objects.get_or_create(
                    lead=lead,
                    description=description,
                    defaults={
                        **details,
                        'type': kind,
                        'status': ActivityStatus.COMPLETED if done else ActivityStatus.PENDING,
                        # A completed one records when and by whom, as completing it in the CRM does.
                        'completed_at': now if done else None,
                        'completed_by': staff if done else None,
                        'created_by': lead.assigned_to or owner,
                    },
                )
                follow_ups_added += added
                missing = {field: value for field, value in details.items() if not getattr(follow_up, field)}
                if missing:
                    Activity.objects.filter(pk=follow_up.pk).update(**missing)

        seeded = Lead.objects.filter(
            country_code='91', phone__in=[phone_for(i) for i in range(len(LEADS))], name__in=[row[0] for row in LEADS],
        )
        statuses = Counter(seeded.values_list('status', flat=True))
        plan_counts = Counter(seeded.values_list('plan__name', flat=True))
        people = Counter(seeded.values_list('assigned_to__name', flat=True))
        self.stdout.write(self.style.SUCCESS(
            f'Demo leads: {created} added, {present} already present, {seeded.count()} in total.'
        ))
        self.stdout.write('By status: ' + ', '.join(f'{label} {statuses[value]}' for value, label in S.choices))
        self.stdout.write('By plan: ' + ', '.join(f'{name} {plan_counts[name]}' for name, _ in PLANS.values()))
        self.stdout.write('Assigned: ' + ', '.join(f'{name or "Unassigned"} {count}' for name, count in people.items()))
        follow_ups = Counter(Activity.objects.filter(
            lead__in=seeded, description__in=[row[3] for row in FOLLOW_UPS],
        ).values_list('status', flat=True))
        self.stdout.write(
            f'Demo follow-ups: {follow_ups_added} added, {sum(follow_ups.values())} in total '
            f'({follow_ups[ActivityStatus.PENDING]} pending, {follow_ups[ActivityStatus.COMPLETED]} completed).'
        )
        self.stdout.write(
            f'Demo Works: {converted} converted now, {seeded.filter(work__isnull=False).count()} Won leads with their Work.'
        )
        if not_converted:
            self.stdout.write(self.style.WARNING(
                f'{len(not_converted)} Won leads could not be converted to Work: {not_converted[0][1]}'
            ))
