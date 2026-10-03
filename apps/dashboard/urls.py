from django.urls import path

from .views import FollowUpsView, RecentView, SummaryView, TimelineView

urlpatterns = [
    path('dashboard/summary/', SummaryView.as_view(), name='dashboard-summary'),
    path('dashboard/follow-ups/', FollowUpsView.as_view(), name='dashboard-follow-ups'),
    path('dashboard/recent/', RecentView.as_view(), name='dashboard-recent'),
    path('dashboard/timeline/', TimelineView.as_view(), name='dashboard-timeline'),
]
