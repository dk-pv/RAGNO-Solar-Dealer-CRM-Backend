from django.urls import path

from . import views

urlpatterns = [
    path('reports/leads/', views.LeadReportView.as_view(), name='report-leads'),
    path('reports/leads/summary/', views.LeadSummaryView.as_view(), name='report-leads-summary'),
    path('reports/works/', views.WorkReportView.as_view(), name='report-works'),
    path('reports/works/summary/', views.WorkSummaryView.as_view(), name='report-works-summary'),
    path('reports/activities/', views.ActivityReportView.as_view(), name='report-activities'),
    path('reports/activities/summary/', views.ActivitySummaryView.as_view(), name='report-activities-summary'),
    path('reports/staff/', views.StaffReportView.as_view(), name='report-staff'),
    path('reports/history/', views.HistoryReportView.as_view(), name='report-history'),
]
