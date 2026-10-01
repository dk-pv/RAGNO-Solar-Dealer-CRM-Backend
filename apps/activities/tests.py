from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, User
from apps.leads.models import Lead, SolarPlan

from .models import Activity

PASSWORD = 'Solar-Panel-2026'


class ActivityAccessTests(APITestCase):
    """A lead's activities can be managed by whoever can work on the lead: an admin, or the staff member it's assigned to."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email='admin@example.com', password=PASSWORD, name='Admin', role=Role.ADMIN)
        cls.staff_a = User.objects.create_user(email='a@example.com', password=PASSWORD, name='Staff A')
        cls.staff_b = User.objects.create_user(email='b@example.com', password=PASSWORD, name='Staff B')
        # The Leads module, as an admin gives it to the STAFF role in Settings -> Roles & Access.
        cls.staff_a.role.permissions.add(*Permission.objects.filter(
            content_type__app_label='leads', codename__in=['view_lead', 'add_lead', 'change_lead'],
        ))
        plan = SolarPlan.objects.get(capacity=5)
        lead = {'phone': '9876543210', 'district': 'Ernakulam', 'plan': plan, 'amount': plan.amount, 'created_by': cls.admin}
        cls.a_lead = Lead.objects.create(name='A Lead', assigned_to=cls.staff_a, **lead)
        cls.b_lead = Lead.objects.create(name='B Lead', assigned_to=cls.staff_b, **lead)

    def as_user(self, user):
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def add(self, lead, **fields):
        body = {'lead': lead.pk, 'type': 'PHONE_CALL', 'description': 'Called about the quote.', **fields}
        return self.client.post(reverse('activity-list'), body, format='json')

    def listed(self, lead):
        response = self.client.get(reverse('activity-list'), {'lead': lead.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [row['description'] for row in response.data['results']]

    def test_admin_manages_activities_on_any_lead(self):
        self.as_user(self.admin)
        created = self.add(self.b_lead)
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual((created.data['type_display'], created.data['created_by_name']), ('Phone call', 'Admin'))
        url = reverse('activity-detail', args=[created.data['id']])
        edited = self.client.patch(url, {'type': 'FOLLOW_UP', 'description': 'Follow-up booked.'}, format='json')
        self.assertEqual((edited.status_code, edited.data['type_display']), (200, 'Follow-up'))
        self.assertEqual(self.listed(self.b_lead), ['Follow-up booked.'])
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.listed(self.b_lead), [])

    def test_assigned_staff_manage_activities_on_their_lead_including_ones_others_added(self):
        self.as_user(self.admin)
        by_admin = self.add(self.a_lead, description="Admin's note.").data['id']
        self.as_user(self.staff_a)
        created = self.add(self.a_lead, type='SITE_VISIT', description='Roof survey done.')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(created.data['created_by_name'], 'Staff A')
        self.assertEqual(self.listed(self.a_lead), ['Roof survey done.', "Admin's note."])
        url = reverse('activity-detail', args=[by_admin])
        self.assertEqual(self.client.patch(url, {'description': 'Corrected note.'}, format='json').status_code, 200)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.listed(self.a_lead), ['Roof survey done.'])

    def test_staff_cannot_see_or_touch_activities_on_another_staff_members_lead(self):
        self.as_user(self.admin)
        on_b = self.add(self.b_lead, description='On B.').data['id']
        self.as_user(self.staff_a)
        self.assertEqual(self.listed(self.b_lead), [])
        refused = self.add(self.b_lead)
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('lead', refused.data)
        url = reverse('activity-detail', args=[on_b])
        self.assertEqual(self.client.patch(url, {'description': 'Changed.'}, format='json').status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Activity.objects.get(pk=on_b).description, 'On B.')

    def test_reading_needs_view_and_writing_needs_change_permission(self):
        self.as_user(self.admin)
        self.add(self.b_lead, description='Visible to B.')
        staff_role = Role.objects.get(name=Role.STAFF)
        staff_role.permissions.remove(Permission.objects.get(content_type__app_label='leads', codename='change_lead'))
        self.as_user(self.staff_b)
        self.assertEqual(self.listed(self.b_lead), ['Visible to B.'])
        self.assertEqual(self.add(self.b_lead).status_code, status.HTTP_403_FORBIDDEN)
        staff_role.permissions.clear()
        self.as_user(self.staff_b)
        self.assertEqual(self.client.get(reverse('activity-list'), {'lead': self.b_lead.pk}).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse('activity-list'), {'lead': self.b_lead.pk}).status_code, 401)

    def test_activities_are_validated(self):
        self.as_user(self.admin)
        self.assertEqual(self.client.get(reverse('activity-list')).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('type', self.add(self.a_lead, type='CONFIRMED').data)
        self.assertIn('description', self.add(self.a_lead, description='').data)
        activity = self.add(self.a_lead).data['id']
        moved = self.client.patch(reverse('activity-detail', args=[activity]), {'lead': self.b_lead.pk}, format='json')
        self.assertEqual(moved.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deleting_a_lead_removes_its_activities(self):
        self.as_user(self.admin)
        self.add(self.a_lead)
        self.assertEqual(self.client.delete(reverse('lead-detail', args=[self.a_lead.pk])).status_code, 204)
        self.assertFalse(Activity.objects.exists())
