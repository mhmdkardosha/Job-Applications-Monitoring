from __future__ import annotations


def review_badge(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from .models import ReviewItem, ReviewStatus

    return {"pending_review_count": ReviewItem.objects.filter(status=ReviewStatus.PENDING).count()}
