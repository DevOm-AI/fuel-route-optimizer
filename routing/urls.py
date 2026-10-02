from django.urls import path

from routing import views

urlpatterns = [
    path('route/', views.route, name='route'),
]
