from django.urls import path

from .views import CrmResetView

urlpatterns = [
    path('maintenance/reset-crm-data/', CrmResetView.as_view(), name='maintenance-reset-crm-data'),
]
