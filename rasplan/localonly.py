"""RASPlanner is a localhost-only tool: requests from other machines are refused."""
from django.http import HttpResponse

LOCAL_ADDRESSES = {"127.0.0.1", "::1", "localhost"}


class LocalOnlyMiddleware:
    """Refuse any request whose REMOTE_ADDR is not this machine. An empty REMOTE_ADDR (internal
    calls, the test client's default plumbing) passes through."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        addr = request.META.get("REMOTE_ADDR", "")
        if addr and addr not in LOCAL_ADDRESSES:
            return HttpResponse("RASPlanner is a localhost-only tool.", status=403)
        return self.get_response(request)
