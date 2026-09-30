from django.contrib import admin

from .models import Lead, SolarPlan


# Until the CRM's Settings screen exists, admins keep plan prices up to date here.
@admin.register(SolarPlan)
class SolarPlanAdmin(admin.ModelAdmin):
    list_display = ['name', 'capacity', 'amount', 'is_active']


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'phone', 'district', 'plan', 'amount', 'status', 'assigned_to', 'created_at']
    list_select_related = ['plan', 'assigned_to']
    list_filter = ['status', 'plan']
    search_fields = ['name', 'phone', 'email']
    # The status only changes through the CRM, which applies the pipeline rules.
    readonly_fields = ['status', 'created_by', 'created_at', 'updated_at']

    def has_add_permission(self, request):
        return False
