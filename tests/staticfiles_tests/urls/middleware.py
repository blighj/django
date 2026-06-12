from django.http import HttpResponse
from django.urls import re_path


def sentinel_view(request, **kwargs):
    return HttpResponse("sentinel")


urlpatterns = [re_path(r"^", sentinel_view)]
